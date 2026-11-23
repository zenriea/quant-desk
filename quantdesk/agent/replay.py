"""Live vs. shadow replay: bar by bar, what the frozen rules would have done and what paper trading did.

The daily review compares totals (live closed 8 trades, the shadow backtest 1). This lines the two up per
signal bar, symbol and side, so the first bar where they diverge can be inspected: the rule values live saw
when it acted, the rule values on today's stored data, when the bar was usable, the expected fill and the
actual fill or the reason there was none.
"""
from __future__ import annotations

import numpy as np

from .. import db, settings
from ..marketdata.freshness import SIP_DELAY, signal_delay_bars
from ..research import backtest as B
from ..research.experiment import build_panel
from ..research.strategy import compute_signals
from ..venues import VENUES

MAX_EXPLAINED = 12  # rule values are recomputed for divergent rows only, the earliest first


def _usable_at(venue: str, bar: int) -> int:
    return bar + VENUES[venue].bar_seconds + (SIP_DELAY if venue == "NASDAQ" else 0)


def _live_orders(sid: int, until: float | None = None) -> list[dict]:
    """Orders created by ``until``, with their fills and status as they stood then."""
    out = []
    cut = until if until is not None else float("inf")
    for o in db.q("SELECT * FROM orders WHERE strategy_id=? AND created_at<=? ORDER BY id", (sid, cut)):
        o = db.row_dict(o, ["signal_snapshot", "risk_check"])
        fills = [dict(f) for f in db.q("SELECT ts, qty, price, price_basis, spread_bps FROM fills WHERE order_id=? "
                                       "AND ts<=? ORDER BY id", (o["id"], cut))]
        if until is not None and (o["closed_at"] or 0) > until:
            o["status"], o["status_note"] = ("partially_filled" if fills else "submitted"), "still open at that time"
        out.append({**o, "fills": fills})
    return out


OPEN = ("intent", "submitted", "partially_filled")


def _outcome(s: dict | None, o: dict | None) -> str:
    """Signal agreement first, then execution: a paper buy the risk engine rejected while the backtest
    filled is a divergence even though both sides signalled on the same bar."""
    if not s:
        return "live_only"
    if not o:
        return "shadow_only"
    if not s["executes"] or (s["fill"] is None and s["skip"] is None) or o["status"] in OPEN:
        return "both"  # one side has not had its chance to fill yet: nothing to compare
    return "both" if bool(s["fill"]) == bool(o["fills"]) else "fill_differs"


def _notes(sid: int, start: float, until: float | None = None) -> list[dict]:
    """Journal entries that explain a missing live decision: data waits, partial bars, downtime, restarts."""
    rows = db.q("SELECT id, ts, kind, title FROM journal WHERE ts>=? AND ("
                "(kind IN ('data','system','decision') AND EXISTS (SELECT 1 FROM json_each(journal.refs) "
                " WHERE json_each.key='strategy_id' AND json_each.value=?)) "
                "OR (kind='system' AND (title='Worker started' OR title LIKE 'Back online after%'))) "
                "AND ts<=? ORDER BY id", (start, sid, until if until is not None else float("inf")))
    return [dict(r) for r in rows]


def replay(strat: dict, until: float | None = None) -> dict:
    """``until`` replays only what was known at that moment, e.g. when a review ran."""
    sid, venue = strat["id"], strat["venue"]
    v = VENUES[venue]
    spec = db.loads(strat["spec"])
    params = spec.get("fixed_params") or {}
    bench = settings.get("benchmarks")[venue]
    panel = build_panel(venue, spec["universe"], bench)
    ts = panel["ts"]
    act = strat["activated_at"] or strat["created_at"]
    end_at = strat["retired_at"] if strat.get("status") == "retired" and strat.get("retired_at") else None
    if until is not None:
        end_at = min(end_at, until) if end_at else until
    start = int(np.searchsorted(ts, act))
    # Only bars that had closed by the cutoff, plus the next bar's open, which is when their signals fill.
    stop = min(len(ts), int(np.searchsorted(ts, end_at - v.bar_seconds, side="right")) + 1) if end_at else len(ts)
    syms = panel["symbols"]
    orders = _live_orders(sid, until)
    base = {"strategy": {k: strat[k] for k in ("id", "name", "version", "venue", "status", "allocation")},
            "span": {"start": act, "end": end_at, "bars": max(0, stop - start), "until": until},
            "timing": {"bar_seconds": v.bar_seconds, "data_delay_seconds": SIP_DELAY if venue == "NASDAQ" else 0,
                       "backtest_fill_delay_bars": signal_delay_bars(venue)}}
    if stop - start < 2:
        return {**base, "rows": [], "first_divergence": None, "notes": _notes(sid, act, until),
                "summary": {"shadow_trades": 0, "live_orders": len(orders), "matched": 0}}
    sig = compute_signals(spec, params, panel)
    events: list[dict] = []
    r = B.run(spec, panel, sig, settings.get("costs")[venue], start, stop, capital=strat["allocation"] or 1.0,
              delay=signal_delay_bars(venue), max_participation=settings.get("risk")["max_participation"], events=events)
    # Shadow decisions keyed by (signal bar, symbol, side); fills follow their signal in order.
    shadow: dict[tuple, dict] = {}
    open_signal: dict[tuple, tuple] = {}
    for e in events:
        sym, bar = syms[e["col"]], int(ts[e["t"]])
        if e["kind"] == "signal":
            key = (bar, sym, e["side"])
            shadow[key] = {"t": e["t"], "reason": e["reason"], "executes": e["executes"], "fill": None, "skip": None}
            open_signal[(sym, e["side"])] = key
        else:
            key = open_signal.pop((sym, e["side"]), None)
            if key and key in shadow:
                if e["kind"] == "fill":
                    shadow[key]["fill"] = {"ts": bar, "px": e["px"], "qty": e["qty"]}
                else:
                    shadow[key]["skip"] = e["reason"]
    live: dict[tuple, dict] = {}
    unkeyed = []
    for o in orders:
        bar = (o.get("signal_snapshot") or {}).get("bar")
        if bar is None:
            unkeyed.append(o)
            continue
        live[(int(bar), o["symbol"], o["side"])] = o
    rows = []
    for key in sorted(set(shadow) | set(live)):
        bar, sym, side = key
        s, o = shadow.get(key), live.get(key)
        f = (o or {}).get("fills") or []
        rows.append({
            "bar": bar, "symbol": sym, "side": side, "usable_at": _usable_at(venue, bar),
            "match": _outcome(s, o),
            "shadow": None if not s else {
                "reason": s["reason"], "executes": s["executes"], "skip": s["skip"], "fill": s["fill"]},
            "live": None if not o else {
                "order_id": o["id"], "status": o["status"], "status_note": o["status_note"], "reason": o["reason"],
                "created_at": o["created_at"], "closed_at": o["closed_at"],
                "rule_values": (o.get("signal_snapshot") or {}).get("rule_values") or [],
                "fill": {"ts": f[-1]["ts"], "px": sum(x["price"] * x["qty"] for x in f) / sum(x["qty"] for x in f),
                         "qty": sum(x["qty"] for x in f), "basis": f[-1]["price_basis"], "spread_bps": f[-1]["spread_bps"]}
                if f else None},
        })
    for o in unkeyed:  # reconcile / flatten orders are not tied to a signal bar
        f = o["fills"]
        rows.append({"bar": None, "symbol": o["symbol"], "side": o["side"], "usable_at": None, "match": "live_only",
                     "shadow": None, "live": {"order_id": o["id"], "status": o["status"], "status_note": o["status_note"],
                                              "reason": o["reason"], "created_at": o["created_at"],
                                              "closed_at": o["closed_at"], "rule_values": [],
                                              "fill": {"ts": f[-1]["ts"], "px": f[-1]["price"], "qty": sum(x["qty"] for x in f),
                                                       "basis": f[-1]["price_basis"], "spread_bps": f[-1]["spread_bps"]}
                                              if f else None}})
    rows.sort(key=lambda x: (x["bar"] if x["bar"] is not None else x["live"]["created_at"], x["symbol"], x["side"]))
    # Rule values on today's stored data for the bars where the two disagree: a different reading than
    # live saw means the stored bar changed (late or revised data), not the rules.
    idx = {int(b): i for i, b in enumerate(ts)}
    market_col = syms.index(bench) if bench in syms else None
    from ..trading.live import explain
    explained = 0
    for row in rows:
        if row["match"] not in ("live_only", "shadow_only") or row["bar"] is None or explained >= MAX_EXPLAINED:
            continue
        t = idx.get(row["bar"])
        expr = spec.get("entry") if row["side"] == "buy" else spec.get("exit")
        if t is None or not expr or row["symbol"] not in syms:
            continue
        row["rule_values_now"] = explain(expr, panel, params, t, syms.index(row["symbol"]), v.periods_per_year, market_col)
        explained += 1
    notes = _notes(sid, act, until)
    first = next((i for i, x in enumerate(rows) if x["match"] != "both"), None)
    if first is not None and rows[first]["bar"] is not None:
        lo, hi = rows[first]["bar"] - v.bar_seconds, rows[first]["usable_at"] + 3 * v.bar_seconds
        rows[first]["nearby_notes"] = [n for n in notes if lo <= n["ts"] <= hi][:8]
    return {**base, "rows": rows, "first_divergence": first, "notes": notes[-40:],
            "summary": {"shadow_trades": r.metrics["n_trades"], "shadow_signals": len(shadow), "live_orders": len(orders),
                        "matched": sum(1 for x in rows if x["match"] == "both"),
                        "shadow_only": sum(1 for x in rows if x["match"] == "shadow_only"),
                        "live_only": sum(1 for x in rows if x["match"] == "live_only"),
                        "fill_differs": sum(1 for x in rows if x["match"] == "fill_differs"),
                        "shadow_return": r.metrics["total_return"]}}
