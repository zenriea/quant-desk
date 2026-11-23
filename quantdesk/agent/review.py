"""Review: compare live paper outcomes with what was expected at approval time.

Deterministic checks run first (and can retire/reduce on their own); the analyst then
explains and may choose continue / reduce / retire / increase within bounds.
"""
from __future__ import annotations

import logging
import math
import traceback

import numpy as np

from .. import clock, db, settings
from ..marketdata.freshness import signal_usable_after
from ..research import backtest as B
from ..research.experiment import build_panel
from ..research.strategy import compute_signals
from ..trading import ledger, live
from ..units import normalize_invalidation
from ..venues import VENUES
from . import journal
from .analyst import RulesAnalyst, get_analyst
from .llm import BudgetExceeded, LLMError

log = logging.getLogger(__name__)


def live_stats(strat: dict) -> dict:
    sid = strat["id"]
    se = ledger.sleeve_equity(sid, strat["allocation"])
    end = strat["retired_at"] if strat.get("status") == "retired" and strat.get("retired_at") else clock.now()
    days = max(0.0, (end - (strat["activated_at"] or strat["created_at"])) / 86400)
    sells = db.q("SELECT f.* FROM fills f JOIN orders o ON o.id=f.order_id WHERE o.strategy_id=? AND f.side='sell'", (sid,))
    fills = db.q1("SELECT COUNT(*) n, COALESCE(SUM(f.fee),0) fees FROM fills f JOIN orders o ON o.id=f.order_id "
                  "WHERE o.strategy_id=?", (sid,))
    ret = (se["equity"] / strat["allocation"] - 1) if strat["allocation"] else 0.0
    slip = db.q1("SELECT AVG(f.slippage_bps) s, AVG(f.spread_bps) sp FROM fills f JOIN orders o ON o.id=f.order_id "
                 "WHERE o.strategy_id=?", (sid,))
    peak = ledger.sleeve_peak(sid, strat["allocation"], se["equity"])
    return {"days_live": round(days, 2), "equity": round(se["equity"], 2), "return": ret,
            "drawdown": (se["equity"] - peak) / peak if peak > 0 else 0.0,
            "realized": round(se["realized"], 2), "unrealized": round(se["unrealized"], 2),
            "fees": round(fills["fees"], 2), "fills": fills["n"], "closed_trades": len({s["order_id"] for s in sells}),
            "avg_slippage_bps": round(slip["s"], 2) if slip["s"] is not None else None,
            "avg_spread_bps": round(slip["sp"], 2) if slip["sp"] is not None else None}


def shadow_backtest(strat: dict) -> dict | None:
    """Replay the frozen rules over the live period (bars since activation)."""
    spec = db.loads(strat["spec"])
    venue = strat["venue"]
    bench = settings.get("benchmarks")[venue]
    panel = build_panel(venue, spec["universe"], bench)
    ts = panel["ts"]
    act = strat["activated_at"] or strat["created_at"]
    start = int(np.searchsorted(ts, act))
    if len(ts) - start < 2:
        return None
    sig = compute_signals(spec, spec.get("fixed_params") or {}, panel)
    r = B.run(spec, panel, sig, settings.get("costs")[venue], start, len(ts), capital=strat["allocation"] or 1.0,
              usable_after=signal_usable_after(venue), max_participation=settings.get("risk")["max_participation"])
    return {"bars": len(ts) - start, "total_return": r.metrics["total_return"], "n_trades": r.metrics["n_trades"],
            "max_drawdown": r.metrics["max_drawdown"]}


def expected_band(strat: dict, days: float) -> dict | None:
    exp = db.loads(strat["expectations"], {})
    t = exp.get("test") or {}
    if not t or not t.get("n_bars"):
        return None
    v = VENUES[strat["venue"]]
    years = days / 365.0
    mu = t.get("cagr", 0.0)
    vol = t.get("ann_vol", 0.0)
    exp_ret = (1 + mu) ** years - 1 if mu > -1 else -1
    sd = vol * math.sqrt(max(years, 1e-9))
    return {"expected_return": exp_ret, "sd": sd, "low_2_5sd": exp_ret - 2.5 * sd, "test_max_drawdown": t.get("max_drawdown"),
            "periods_per_year": v.periods_per_year}


def review_strategy(strat: dict, analyst=None) -> dict:
    analyst = analyst or get_analyst()
    st = live_stats(strat)
    band = expected_band(strat, st["days_live"])
    inval = normalize_invalidation(db.loads(strat["invalidation"], {}) or {})
    flags = []
    max_dd = inval.get("max_live_drawdown_pct")
    if max_dd and st["drawdown"] <= -max_dd:
        flags.append(f"live drawdown {st['drawdown']:.1%} breached the recorded limit -{max_dd:.1%}")
    if band and st["closed_trades"] >= (inval.get("min_trades_before_judging") or 5) and st["return"] < band["low_2_5sd"]:
        flags.append(f"live return {st['return']:+.2%} is below the expectation band low {band['low_2_5sd']:+.2%}")
    try:
        shadow = shadow_backtest(strat)
    except Exception as e:
        shadow = {"error": str(e)[:200]}
    rs = settings.get("research")
    allowed_increase = (st["days_live"] >= rs["min_live_days_before_increase"] and not flags
                        and strat["allocation"] < strat["approved_allocation"] - 1)
    ctx = {"strategy": {k: strat[k] for k in ("id", "name", "version", "venue", "allocation", "approved_allocation",
                                              "thesis", "exit_plan")},
           "invalidation": inval, "expectations": db.loads(strat["expectations"], {}), "expected_band": band,
           "live": st, "shadow_backtest": shadow, "auto_flags": flags, "allowed_increase": allowed_increase,
           "positions": ledger.sleeve_positions(strat["id"]),
           "recent_fills": [dict(r) for r in db.q("SELECT f.* FROM fills f JOIN orders o ON o.id=f.order_id WHERE "
                                                  "o.strategy_id=? ORDER BY f.id DESC LIMIT 10", (strat["id"],))]}
    try:
        rev, call_id = analyst.review(ctx)
        author = analyst.author
    except (BudgetExceeded, LLMError):
        rev, call_id = RulesAnalyst().review(ctx)
        author = "rules"
    action = rev["action"]
    # deterministic overrides: a breached drawdown limit always retires
    if any("drawdown" in f for f in flags):
        action = "retire"
    if action == "increase" and not allowed_increase:
        action = "continue"
    eq = ledger.equity_breakdown()["equity"]
    new_alloc = strat["allocation"]
    if action == "reduce":
        req = rev.get("new_allocation_pct")
        new_alloc = min(strat["allocation"] * 0.5, req * eq) if req else strat["allocation"] * 0.5
    elif action == "increase":
        req = rev.get("new_allocation_pct") or strat["approved_allocation"] / eq
        new_alloc = min(req * eq, strat["approved_allocation"], settings.get("risk")["max_strategy_pct"] * eq,
                        strat["allocation"] + live.allocation_room(eq))  # never beyond the total-allocation limit
        new_alloc = max(new_alloc, strat["allocation"])
    did = db.ex("INSERT INTO decisions(ts, kind, subject_type, subject_id, rationale, details, author, llm_call_id) "
                "VALUES(?,?,?,?,?,?,?,?)", (clock.now(), f"review_{action}", "strategy", strat["id"], rev["journal"],
                                            db.dumps({"live": st, "shadow": shadow, "flags": flags, "band": band,
                                                      "lessons": rev.get("lessons", []), "new_allocation": new_alloc}),
                                            author, call_id))
    if action == "retire":
        live.retire_strategy(strat, f"Retiring {strat['name']} v{strat['version']}: " + (
            "; ".join(flags) if flags else "analyst review concluded the thesis no longer holds."))
        db.ex("UPDATE strategies SET last_review_at=? WHERE id=?", (clock.now(), strat["id"]))
        title = f"Retired {strat['name']} v{strat['version']}"
    else:
        with db.tx():
            ledger.mark_sleeve_peak(strat["id"], strat["allocation"], new_alloc)
            db.ex("UPDATE strategies SET allocation=?, last_review_at=? WHERE id=?", (new_alloc, clock.now(), strat["id"]))
        title = {"continue": f"Review: keeping {strat['name']} v{strat['version']}",
                 "reduce": f"Reduced exposure: {strat['name']} v{strat['version']}",
                 "increase": f"Increased allocation: {strat['name']} v{strat['version']}"}[action]
    body = rev["journal"]
    if action in ("reduce", "increase"):
        body += f"\nAllocation ${strat['allocation']:,.0f} -> ${new_alloc:,.0f} (applies to new entries)."
    journal.write("review", title, body, refs={"strategy_id": strat["id"], "decision_id": did}, author=author,
                  llm_call_id=call_id)
    from .research_loop import _save_beliefs
    _save_beliefs(rev.get("beliefs", []), strat["venue"], author, call_id)
    return {"action": action, "flags": flags}


def review_all(analyst=None) -> list[dict]:
    """Review every live strategy; one failing review never blocks the others or fails silently."""
    out = []
    for s in db.q("SELECT * FROM strategies WHERE status IN ('active','incubating')"):
        s = dict(s)
        try:
            out.append(review_strategy(s, analyst))
        except Exception as e:
            log.error("review of strategy %s failed: %s", s["id"], traceback.format_exc())
            journal.write("system", f"Review of {s['name']} v{s['version']} failed",
                          f"{type(e).__name__}: {e}"[:1500] + "\n\nThe strategy keeps trading under its frozen rules and "
                          "the risk engine; the review is retried tomorrow.", refs={"strategy_id": s["id"]},
                          author="system")
    return out
