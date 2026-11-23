"""Observe -> Investigate -> Test -> Challenge -> Decide.

Each stage is persisted before the next starts, so the record shows what was believed
and decided *before* outcomes were known:
  hypothesis row (rationale, prediction, falsification) -> experiment row (frozen spec)
  -> test results -> decision row (+ lessons) -> strategy row (thesis, exit plan,
  invalidation) if capital is allocated.
"""
from __future__ import annotations

import logging
import re
import traceback

from .. import clock, db, settings
from ..config import load_config
from ..research import gates as G
from ..research.experiment import ExperimentError, run_experiment, validate_research_spec
from ..research.observe import observe
from ..research.sandbox import python_strategies_allowed
from ..research.strategy import SpecError, describe_rules, spec_hash
from ..trading import ledger
from . import journal
from .analyst import RulesAnalyst, get_analyst, memory, spec_from_llm
from .llm import BudgetExceeded, LLMError

log = logging.getLogger(__name__)


def _fmt_obs(obs: dict) -> str:
    if obs.get("error"):
        return obs["error"]
    n = obs.get("notable") or []
    return "; ".join(x["text"] for x in n[:6]) if n else "no unusual moves, volatility shifts or regime changes"


def record_observation(venue: str) -> dict:
    u = settings.get("universe")[venue]
    bench = settings.get("benchmarks")[venue]
    obs = observe(venue, u, bench)
    oid = db.ex("INSERT INTO observations(ts, venue, data_end, summary, notable) VALUES(?,?,?,?,?)",
                (clock.now(), venue, obs.get("as_of"), db.dumps(obs), db.dumps(obs.get("notable"))))
    obs["observation_id"] = oid
    if obs.get("notable"):
        journal.write("observation", f"{venue}: what changed",
                      "From stored bars: " + _fmt_obs(obs) + ".", refs={"observation_id": oid},
                      author="rules", dedupe_key=f"obs:{venue}:{obs.get('as_of')}")
    return obs


def portfolio_summary() -> dict:
    eb = ledger.equity_breakdown()
    strats = [dict(r) for r in db.q("SELECT id, name, version, venue, status, allocation, approved_allocation FROM strategies "
                                    "WHERE status IN ('active','incubating')")]
    return {"equity": round(eb["equity"], 2), "cash": round(eb["cash"], 2), "gross_exposure": round(eb["gross"], 2),
            "positions": {s: {"qty": v["qty"], "value": round(v["value"], 2)} for s, v in eb["positions"].items()},
            "active_strategies": strats}


def _prior_test_windows(venue: str, days: int) -> list[tuple[int, int]]:
    """Test windows of recent experiments, used to deflate significance for holdout reuse.

    Only experiments from the last ``research.deflation_window_days`` count: counting every
    experiment ever run makes the deflated-PSR bar rise without limit as the desk keeps working."""
    since = clock.now() - days * 86400
    out = []
    for r in db.q("SELECT windows FROM experiments WHERE venue=? AND frozen_at IS NOT NULL AND test IS NOT NULL "
                  "AND created_at >= ?", (venue, since)):
        w = db.loads(r["windows"], {})
        if w and "test" in w:
            out.append((w["test"]["start"], w["test"]["end"]))
    return out


def bar_clock(venue: str) -> dict:
    """What a window of N bars means in calendar time on this venue (from venues.py)."""
    from ..venues import VENUES
    v = VENUES[venue]
    per_week = v.bars_per_day * (5 if v.asset_class == "equity" else 7)
    return {"bar_size": v.timeframe, "bar_minutes": v.bar_seconds // 60,
            "bars_per_session" if v.asset_class == "equity" else "bars_per_day": v.bars_per_day,
            "bars_per_week": per_week, "bars_per_year": v.periods_per_year,
            "note": ("Stock sessions are 6.5 hours; windows skip nights, weekends and holidays." if v.asset_class == "equity"
                     else "Crypto trades 24/7.")}


MECHANISM_STREAK = 3   # this many consecutive finished experiments on one mechanism forces a different one
# The cost-stress gate doubles fees, spread and impact, so an edge only just above one round trip fails it.
# Live paper spreads on IEX quotes have also run several times the modelled half-spread.
EDGE_COST_MULTIPLE = 2.0


def validate_trade_economics(prop: dict, spec: dict, venue: str, round_trip_bps: float) -> None:
    """Pre-experiment checks on an LLM proposal: a stated holding period, an edge per trade above the
    round-trip cost, lookbacks explained in time, and no fourth test in a row of one mechanism."""
    from .analyst import mechanism_tags
    hold, edge = prop.get("holding_period_bars"), prop.get("edge_bps_per_trade")
    if not isinstance(hold, int) or hold < 1:
        raise SpecError("State holding_period_bars: the typical number of bars a trade is held.")
    if not isinstance(edge, (int, float)) or edge < EDGE_COST_MULTIPLE * round_trip_bps:
        raise SpecError(f"edge_bps_per_trade ({edge}) must be at least {EDGE_COST_MULTIPLE:g}x the round-trip cost of "
                        f"about {round_trip_bps:.0f} bp (so at least {EDGE_COST_MULTIPLE * round_trip_bps:.0f} bp) at the "
                        f"stated holding period of {hold} bars: the gates double costs, and live spreads run wider "
                        "than modelled. Hold longer so each trade can earn that, or answer no_idea.")
    if not str(prop.get("lookbacks_in_time") or "").strip():
        raise SpecError(f"Explain each lookback in calendar time in lookbacks_in_time ({bar_clock(venue)}).")
    tags = mechanism_tags(spec)
    recent = [mechanism_tags(db.loads(r["spec"], {})) for r in db.q(
        "SELECT spec FROM experiments WHERE venue=? AND status IN ('passed','failed') ORDER BY id DESC LIMIT ?",
        (venue, MECHANISM_STREAK))]
    if len(recent) == MECHANISM_STREAK:
        repeated = tags & set.intersection(*recent)
        if repeated:
            raise SpecError(f"The last {MECHANISM_STREAK} {venue} experiments all used {', '.join(sorted(repeated))}. "
                            "Propose a different mechanism or answer no_idea; a new window is not a new mechanism.")


def record_unexpected_failure(venue: str, exc: BaseException) -> None:
    """A research cycle crashed (a bug, not a provider outage): make it visible in the journal
    and close out any experiment it left half-done, so nothing fails silently."""
    now = clock.now()
    for e in db.q("SELECT id, hypothesis_id FROM experiments WHERE status='running' AND venue=?", (venue,)):
        db.ex("UPDATE experiments SET status='error', error=?, completed_at=? WHERE id=?",
              (f"{type(exc).__name__}: {exc}"[:1000], now, e["id"]))
        db.ex("UPDATE hypotheses SET status='abandoned', updated_at=? WHERE id=? AND status='testing'", (now, e["hypothesis_id"]))
    journal.write("system", f"{venue} research cycle failed unexpectedly",
                  f"{type(exc).__name__}: {exc}"[:1500] + "\n\nThis is a software error, not a market or AI-provider "
                  "problem. The cycle was abandoned and the next one runs on schedule. Details are in the worker log "
                  "and Limits & system > Jobs.", author="system")


def _record_no_idea(venue: str, prop: dict, author: str, call_id, observation_id: int) -> dict:
    # Snapshot cited checks so later gate changes cannot rewrite the journal.
    ids: list[int] = []
    for m in re.finditer(r"\b(?:exps?|experiments?)(?:[:#]|\s+#?)(\d{1,18}(?:\s*,\s*(?:[:#]\s*)?\d{1,18})*)", prop["why"], re.I):
        ids.extend(int(n) for n in re.findall(r"\d{1,18}", m.group(1)))
    ids = list(dict.fromkeys(ids))[:32]
    evidence = []
    for eid in ids:
        row = db.q1("SELECT gates FROM experiments WHERE id=? AND venue=? AND status IN ('passed','failed')",
                    (eid, venue))
        if row is None:
            continue
        checks = (db.loads(row["gates"], {}) or {}).get("checks") or []
        gate = next((g for g in checks if g.get("name") == "test_trades" and g.get("op") == ">="), {})
        count, minimum = gate.get("value"), gate.get("threshold")
        if type(count) is int and type(minimum) is int:
            evidence.append({"experiment_id": eid, "trades": count, "minimum": minimum})
    journal.write("research", f"{venue}: no experiment proposed", prop["why"], author=author,
                  llm_call_id=call_id, refs={"observation_id": observation_id, "trade_checks": evidence})
    db.ex("INSERT INTO decisions(ts, kind, subject_type, subject_id, rationale, details, author, llm_call_id) "
          "VALUES(?,?,?,?,?,?,?,?)", (clock.now(), "no_trade", "venue", None, prop["why"],
                                    db.dumps({"venue": venue}), author, call_id))
    return {"status": "no_idea"}


def research_cycle(venue: str, analyst=None) -> dict:
    cfg = load_config()
    analyst = analyst or get_analyst()
    rs = settings.get("research")
    allowed = settings.get("universe")
    bench = settings.get("benchmarks")[venue]
    journal.set_status("researching", f"{venue}: observing markets")
    obs = record_observation(venue)
    if obs.get("error"):
        journal.write("data", f"{venue}: research skipped",
                      f"Not enough stored market data to research ({obs['error']}). Waiting for data.",
                      author="rules", dedupe_key=f"noresearch:{venue}:{int(clock.now() // 3600)}")
        return {"status": "no_data"}
    blinded = observe(venue, allowed[venue], bench, blind_frac=rs["train_frac"] + rs["val_frac"]) \
        if rs.get("observation_blinding", True) else obs
    costs = settings.get("costs")[venue]
    round_trip = 2 * (costs.get("taker_fee_bps", 0) + costs["backtest_half_spread_bps"] + costs["base_slippage_bps"])
    ctx = {
        # without these the model proposes high-turnover ideas that lose to fees and can never pass the gates
        "trading_costs": dict(costs, approx_round_trip_bps=round_trip,
                              min_edge_bps_per_trade=round(EDGE_COST_MULTIPLE * round_trip, 1),
                              note="Every buy+sell pays about approx_round_trip_bps plus market impact. "
                                   "edge_bps_per_trade must be at least min_edge_bps_per_trade."),
        "acceptance_gates": settings.get("gates"),
        "venue": venue, "approved_universe": allowed[venue], "benchmark": bench,
        "timeframe": obs["timeframe"], "market_context": blinded,
        "context_note": ("Blinded: statistics end at the earliest possible test-window start." if blinded is not obs
                         else "Unblinded current statistics (operator disabled blinding)."),
        "bar_clock": bar_clock(venue),
        "research_memory": memory(venue),
        "active_strategies": portfolio_summary()["active_strategies"],
        "limits": {"max_param_combos": rs["max_param_combos"]},
        "python_strategies_allowed": python_strategies_allowed(),
        "now": clock.now(),
    }
    journal.set_status("researching", f"{venue}: forming a hypothesis")
    try:
        prop, call_id = analyst.propose(ctx)
    except BudgetExceeded as e:
        journal.write("system", "Research paused: API budget", f"{e}. Deterministic trading and risk controls continue.",
                      author="system", dedupe_key=f"budget:{int(clock.now() // 3600)}")
        return {"status": "budget"}
    except LLMError as e:
        journal.write("system", "Research call failed", f"{e}. No hypothesis this cycle.", author="system")
        return {"status": "llm_error"}
    author = analyst.author
    if prop["decision"] == "no_idea" or not prop.get("spec"):
        return _record_no_idea(venue, prop, author, call_id, obs["observation_id"])
    spec = spec_from_llm(prop["spec"], venue) if author == "llm" else prop["spec"]
    for attempt in range(2):
        try:
            validate_research_spec(spec, allowed, rs["max_param_combos"])
            if spec.get("kind") == "code" and not ctx["python_strategies_allowed"]:
                raise SpecError("Python strategy execution is unavailable. Use kind='dsl'.")
            if author == "llm":
                validate_trade_economics(prop, spec, venue, round_trip)
            break
        except SpecError as e:
            if attempt == 0 and author == "llm":
                try:
                    prop, call_id = analyst.repair(ctx, prop, str(e))
                    if prop["decision"] == "no_idea" or not prop.get("spec"):
                        return _record_no_idea(venue, prop, author, call_id, obs["observation_id"])
                    spec = spec_from_llm(prop["spec"], venue)
                    continue
                except (LLMError, BudgetExceeded) as e2:
                    err = str(e2)
            else:
                err = str(e)
            journal.write("research", f"Discarded an invalid strategy spec ({venue})",
                          f"The proposed rules could not be validated: {err}. Nothing was tested.", author="rules",
                          llm_call_id=call_id)
            return {"status": "invalid_spec", "error": err}
    now = clock.now()
    hid = db.ex("INSERT INTO hypotheses(created_at, venue, title, rationale, prediction, falsification, status, parent_id, "
                "observation_id, author, llm_call_id, updated_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
                (now, venue, prop["title"], prop["rationale"], prop["prediction"], prop["falsification"], "testing",
                 prop.get("parent_experiment_id"), obs["observation_id"], author, call_id, now))
    rules = describe_rules(spec, {})
    journal.write("hypothesis", f"Investigating: {prop['title']}",
                  f"{prop['why']}\n\nIdea: {prop['rationale']}\nPrediction: {prop['prediction']}\n"
                  f"I'll drop it if: {prop['falsification']}\nRules: entry `{rules['entry']}`, exit `{rules['exit']}`"
                  + (f", rank by `{rules['rank_by']}` top {rules['top_k']}" if rules.get("rank_by") else "")
                  + f"; universe {', '.join(spec['universe'])}; parameter grid {spec.get('params') or {}}.",
                  refs={"hypothesis_id": hid, "observation_id": obs["observation_id"]}, author=author, llm_call_id=call_id)
    from ..venues import VENUES
    eid = db.ex("INSERT INTO experiments(hypothesis_id, created_at, venue, kind, spec, spec_hash, status, bar_seconds) "
                "VALUES(?,?,?,?,?,?,?,?)", (hid, now, venue, spec.get("kind", "dsl"), db.dumps(spec), spec_hash(spec),
                                            "running", VENUES[venue].bar_seconds))
    journal.set_status("testing", f"Experiment {eid}: {prop['title']}")

    def on_freeze(fr):
        db.ex("UPDATE experiments SET frozen_at=?, chosen_params=?, windows=? WHERE id=?",
              (clock.now(), db.dumps(fr["chosen_params"]), db.dumps(fr["windows"]), eid))

    try:
        days = rs["deflation_window_days"]
        res = run_experiment(spec, settings.get("costs")[venue], rs, bench, _prior_test_windows(venue, days),
                             cfg.llm_knowledge_cutoff, on_freeze=on_freeze, llm_authored=(author == "llm"),
                             max_participation=settings.get("risk")["max_participation"])
        res["leakage"]["deflation_window_days"] = days
    except (ExperimentError, Exception) as e:
        log.error("experiment %s failed: %s", eid, traceback.format_exc())
        db.ex("UPDATE experiments SET status='error', error=?, completed_at=? WHERE id=?", (str(e)[:1000], clock.now(), eid))
        db.ex("UPDATE hypotheses SET status='abandoned', updated_at=? WHERE id=?", (clock.now(), hid))
        journal.write("test", f"Experiment {eid} could not run", f"{type(e).__name__}: {e}", refs={"experiment_id": eid},
                      author="rules")
        return {"status": "error", "error": str(e)}
    gres = G.evaluate(res, settings.get("gates"))
    db.ex("UPDATE experiments SET grid_results=?, train=?, validation=?, test=?, challenge=?, gates=?, leakage=?, "
          "equity_curve=?, status=?, completed_at=? WHERE id=?",
          (db.dumps(res["grid_results"]), db.dumps(res["train"]), db.dumps(res["validation"]), db.dumps(res["test"]),
           db.dumps(res["challenge"]), db.dumps(gres), db.dumps(res["leakage"]), db.dumps(res["equity_curve"]),
           "passed" if gres["passed"] else "failed", clock.now(), eid))
    t = res["test"]
    bvenue = t.get("benchmark_venue") or {}
    journal.write("test", f"Test result: {prop['title']} ({'passed' if gres['passed'] else 'failed'} gates)",
                  f"Chosen on train: {res['chosen_params']}. Train Sharpe {res['train']['sharpe']:.2f}, validation "
                  f"{res['validation']['sharpe']:.2f}. Sealed test ({res['windows']['test']['bars']} bars): net return "
                  f"{t['total_return']:+.2%}, Sharpe {t['sharpe']:.2f}, max DD {t['max_drawdown']:.1%}, {t['n_trades']} trades, "
                  f"cost drag {t['cost_drag']:.2%}; {bench} buy-and-hold {bvenue.get('total_return', 0):+.2%}. "
                  f"Doubled costs: {res['challenge']['test_cost_x2']['total_return']:+.2%}; one-bar delay Sharpe "
                  f"{res['challenge']['test_delay_1bar']['sharpe']:.2f}. Deflated PSR {t['deflated_psr']:.2f} "
                  f"({res['leakage']['n_trials_for_deflation']} trial(s) on this holdout)."
                  + (f" Failed: {', '.join(gres['failed'])}." if gres["failed"] else ""),
                  refs={"experiment_id": eid, "hypothesis_id": hid}, author="rules")
    return _decide(venue, eid, hid, prop, spec, res, gres, obs, bench, rs, analyst)


def _decide(venue, eid, hid, prop, spec, res, gres, obs, bench, rs, analyst) -> dict:
    # ---- Decide (spec is frozen; unblinded context is now allowed)
    journal.set_status("deciding", f"Experiment {eid}")
    lim = settings.get("risk")
    dctx = {"experiment_id": eid, "hypothesis": {"id": hid, "title": prop["title"], "rationale": prop["rationale"],
                                                  "prediction": prop["prediction"], "falsification": prop["falsification"]},
            "spec": spec, "results": {k: res[k] for k in ("windows", "chosen_params", "train", "validation", "test",
                                                          "challenge", "leakage")},
            "gates": gres, "benchmark": bench, "current_market": obs, "portfolio": portfolio_summary(),
            "limits": {"max_strategy_pct": lim["max_strategy_pct"], "max_total_allocation_pct": lim["max_total_allocation_pct"],
                       "incubation_fraction": rs["incubation_fraction"]}}
    try:
        dec, dcall = analyst.decide(dctx)
        dauthor = analyst.author
    except (BudgetExceeded, LLMError) as e:
        dec, dcall = RulesAnalyst().decide(dctx)
        dauthor = "rules"
        journal.write("system", "Decision made by rules engine", f"LLM unavailable for the decision step ({e}); "
                      "the deterministic rules made it instead.", author="system")
    decision = dec["decision"]
    if decision == "allocate" and not gres["passed"]:
        decision = "reject"
        dec["journal"] += "\n\n[Risk/gate engine] Allocation refused: gates failed (" + ", ".join(gres["failed"]) + ")."
    elif gres["passed"] and (decision != "allocate" or not float(dec.get("allocation_pct") or 0) > 0):
        # The gates decide admission; the analyst only sizes. A passing test always gets paper capital.
        floor = min(GATE_PASS_MIN_PCT, lim["max_strategy_pct"])
        dec["journal"] += (f"\n\n[Gate engine] Every gate passed, so this idea is admitted to paper trading at "
                           f"{floor:.0%} of equity (incubation size first). The analyst's view ('{decision}') is "
                           "recorded above; it sets the size but cannot block a passing test.")
        dec["allocation_pct"] = floor
        dec["thesis"] = dec.get("thesis") or prop["rationale"]
        dec["exit_plan"] = dec.get("exit_plan") or DEFAULT_EXIT_PLAN
        decision = "allocate"
    did = db.ex("INSERT INTO decisions(ts, kind, subject_type, subject_id, rationale, details, author, llm_call_id) "
                "VALUES(?,?,?,?,?,?,?,?)", (clock.now(), decision, "experiment", eid, dec["journal"],
                                            db.dumps({"lessons": dec.get("lessons", []), "revision_idea": dec.get("revision_idea"),
                                                      "requested_allocation_pct": dec.get("allocation_pct")}),
                                            dauthor, dcall))
    _save_beliefs(dec.get("beliefs", []), venue, dauthor, dcall)
    hstatus = {"reject": "rejected", "revise": "revise", "allocate": "allocated"}[decision]
    db.ex("UPDATE hypotheses SET status=?, updated_at=? WHERE id=?", (hstatus, clock.now(), hid))
    missed = len(gres["failed"])
    gate_note = f" (missed {missed} gate{'s' if missed != 1 else ''})" if missed else ""
    title = {"reject": f"Not promoted{gate_note}: {prop['title']}", "revise": f"Revising: {prop['title']}",
             "allocate": f"Allocating capital: {prop['title']}"}[decision]
    body = dec["journal"]
    if dec.get("lessons"):
        body += "\n\nRecorded lessons: " + " | ".join(dec["lessons"])
    if decision == "revise" and dec.get("revision_idea"):
        body += f"\nNext: {dec['revision_idea']}"
    evidence = {"outcome": decision, "test_return": res["test"].get("total_return"),
                "test_trades": res["test"].get("n_trades"), "gates_passed": gres["passed"],
                "failed_gates": gres["failed"]}
    journal.write("decision", title, body,
                  refs={"experiment_id": eid, "decision_id": did, "decision_evidence": evidence},
                  author=dauthor, llm_call_id=dcall)
    out = {"status": "done", "experiment_id": eid, "decision": decision}
    if decision == "allocate":
        out["strategy_id"] = create_strategy(hid, eid, prop, spec, res, dec, dauthor, dcall)
    else:
        sid = auto_probe(venue, eid)
        if sid:
            out["probe_strategy_id"] = sid
    return out


# ------------------------------------------------------------------ probes
GATE_PASS_MIN_PCT = 0.05
DEFAULT_EXIT_PLAN = "Exit per the tested rules; retire if live drawdown exceeds 1.5x the test drawdown."


def bar_size_mismatch(e: dict) -> str | None:
    """Rule windows are counted in bars, so an experiment run on another bar size (e.g. daily, before
    the switch to 15-minute bars) tests a different strategy than the one that would trade now.
    New rows store bar_seconds; older rows fall back to the test window's calendar span per bar."""
    from ..venues import VENUES
    v = VENUES.get(e.get("venue"))
    if v and e.get("bar_seconds"):
        if e["bar_seconds"] == v.bar_seconds:
            return None
        return (f"it was tested on a different bar size ({e['bar_seconds'] // 60}-minute bars vs {v.timeframe} now), "
                "so its rules would mean something else live")
    t = (db.loads(e.get("windows"), {}) or {}).get("test") or {}
    if not v or not t.get("bars") or not t.get("end") or t["end"] <= t.get("start", 0):
        return None
    tested = (t["end"] - t["start"]) / t["bars"]
    expected = 365.25 * 86400 / v.periods_per_year
    if tested <= 3 * expected:   # only coarser bars are flagged: demo data has no overnight gaps
        return None
    return (f"it was tested on a different bar size (~{tested / 3600:.1f}h of calendar time per bar vs "
            f"~{expected / 3600:.1f}h for {v.timeframe} bars now), so its rules would mean something else live")


def probe_blocker(e: dict, operator: bool = False) -> str | None:
    """Why experiment row ``e`` cannot start a probe (None = it can)."""
    p = settings.get("probe")
    if not e or e["status"] not in ("failed", "passed") or not e["test"]:
        return "only finished experiments with test results can be probed"
    stale = bar_size_mismatch(e)
    if stale:
        return stale
    existing = db.q1("SELECT probe, status FROM strategies WHERE experiment_id=? ORDER BY id DESC", (e["id"],))
    if existing:
        kind = "probe" if existing["probe"] else "strategy"
        if existing["status"] in ("active", "incubating"):
            return f"already trading as a {kind}"
        if not operator:  # only the operator may re-run a retired idea; the desk never re-probes on its own
            return f"already ran as a {kind} ({existing['status']})"
    if settings.get("agent").get("emergency_stop"):
        return "emergency stop is active"
    if operator:
        return None
    if not p["enabled"]:
        return "probes are switched off"
    t = db.loads(e["test"], {}) or {}
    if t.get("total_return") is None or t["total_return"] <= p["min_test_return"]:
        return f"sealed-test return {t.get('total_return') or 0:+.2%} is not above {p['min_test_return']:+.2%}"
    if (t.get("n_trades") or 0) < settings.get("gates")["min_test_trades"]:
        return "too few test trades"
    active = db.q1("SELECT COUNT(*) n FROM strategies WHERE probe=1 AND status IN ('active','incubating')")["n"]
    if active >= p["max_active"]:
        return f"{active} probe(s) already running (limit {p['max_active']})"
    today = db.q1(
        "SELECT COUNT(*) n FROM strategies s WHERE s.probe=1 AND s.venue=? AND s.created_at>=? "
        "AND NOT EXISTS (SELECT 1 FROM decisions d WHERE d.subject_type='experiment' "
        "AND d.subject_id=s.experiment_id AND d.kind='probe' AND d.author='operator' "
        "AND json_extract(d.details, '$.strategy_id')=s.id)",
        (e["venue"], clock.now() - 86400))["n"]
    if today >= p["per_venue_per_day"]:
        return f"{e['venue']} already started {today} probe(s) in the last 24 hours"
    return None


def ranked_candidates(since: float) -> list[dict]:
    """Experiments with a sealed-test result since ``since``, closest to trading first: current bar size,
    then passed but not allocated, then probe-eligible, then the rest; ties by test return.
    The API pipeline, the UI verdict and the worker status all read this one order."""
    out = []
    for r in db.q("SELECT e.*, h.title FROM experiments e JOIN hypotheses h ON h.id=e.hypothesis_id "
                  "WHERE e.created_at>=? ORDER BY e.id DESC", (since,)):
        e = dict(r)
        t = db.loads(e["test"], {}) or {}
        if t.get("total_return") is None:
            continue
        g = db.loads(e["gates"], {}) or {}
        live = db.q1("SELECT 1 FROM strategies WHERE experiment_id=? AND probe=0 AND status IN ('active','incubating')", (e["id"],))
        leakage = db.loads(e["leakage"], {}) or {}
        out.append({"id": e["id"], "title": e["title"], "venue": e["venue"], "status": e["status"],
                    "test_return": t["total_return"], "sharpe": t.get("sharpe"), "trades": t.get("n_trades"),
                    "holdout_reuse_count": leakage.get("holdout_reuse_count"),
                    "deflation_window_days": leakage.get("deflation_window_days"),
                    "failed": g.get("failed") or [], "n_checks": len(g.get("checks") or []) or 9,
                    "passed": e["status"] == "passed", "allocated": bool(live),
                    "bar_size_mismatch": bar_size_mismatch(e),
                    "probe_blocker": probe_blocker(e), "operator_blocker": probe_blocker(e, operator=True)})
    out.sort(key=lambda c: (c["bar_size_mismatch"] is not None, not (c["passed"] and not c["allocated"]),
                            c["probe_blocker"] is not None, -c["test_return"]))
    return out


def auto_probe(venue: str, eid: int) -> int | None:
    e = db.q1("SELECT * FROM experiments WHERE id=?", (eid,))
    if probe_blocker(dict(e) if e else None) is not None:
        return None
    return start_probe(eid, origin="rules")


def start_probe(eid: int, origin: str = "operator") -> int | None:
    """Start a small, clearly-labelled forward-paper trial of a finished experiment."""
    row = db.q1("SELECT * FROM experiments WHERE id=?", (eid,))
    e = dict(row) if row else None
    why = probe_blocker(e, operator=(origin == "operator"))
    if why:
        journal.write("system", f"Experiment {eid}: probe not started", why[0].upper() + why[1:] + ".", author="system",
                      refs={"experiment_id": eid})
        return None
    p = settings.get("probe")
    h = dict(db.q1("SELECT * FROM hypotheses WHERE id=?", (e["hypothesis_id"],)))
    res = {k: db.loads(e[k]) for k in ("windows", "chosen_params", "train", "validation", "test", "challenge", "leakage")}
    spec = db.loads(e["spec"])
    gates = db.loads(e["gates"], {}) or {}
    t = res["test"]
    who = "You asked" if origin == "operator" else "The desk chose"
    failed = ", ".join(gates.get("failed") or []) or "none"
    dec = {"allocation_pct": p["allocation_pct"],
           "thesis": f"PROBE (forward paper test, not a validated edge). {h['rationale']}",
           "exit_plan": (f"Exit per the tested rules. Retired automatically after {p['lifetime_days']} days; the daily "
                         "review can retire it earlier."),
           "invalidation": {"max_live_drawdown_pct": round(min(0.25, max(0.05, abs(t.get("max_drawdown") or 0.1) * 1.5)), 3),
                            "review_after_days": p["lifetime_days"], "min_trades_before_judging": 5,
                            "conditions": "Probe: judged on live results only."}}
    prop = {k: h[k] for k in ("title", "rationale", "prediction", "falsification")}
    sid = create_strategy(h["id"], eid, prop, spec, res, dec, "operator" if origin == "operator" else "rules", None,
                          probe=True)
    db.ex("INSERT INTO decisions(ts, kind, subject_type, subject_id, rationale, details, author, llm_call_id) "
          "VALUES(?,?,?,?,?,?,?,?)", (clock.now(), "probe", "experiment", eid,
                                      f"{who} to paper-trade this idea as a probe. Sealed test: {t.get('total_return', 0):+.2%} "
                                      f"net, Sharpe {t.get('sharpe') or 0:.2f}, {t.get('n_trades')} trades. Gates not passed: "
                                      f"{failed}.", db.dumps({"strategy_id": sid}),
                                      "operator" if origin == "operator" else "rules", None))
    return sid


def expire_probes() -> list[int]:
    """Probes are short trials: retire them when their lifetime ends."""
    from ..trading.live import retire_strategy
    days = settings.get("probe")["lifetime_days"]
    out = []
    for s in db.q("SELECT * FROM strategies WHERE probe=1 AND status IN ('active','incubating') AND created_at < ?",
                  (clock.now() - days * 86400,)):
        retire_strategy(dict(s), "probe period ended", origin="system")
        journal.write("decision", f"Probe finished: {s['name']} v{s['version']}",
                      f"The {days}-day probe period ended, so its positions are being closed. Its live record stays in "
                      "Strategies for comparison with the backtest.", refs={"strategy_id": s["id"]}, author="system")
        out.append(s["id"])
    return out


def rejudge_experiment(eid: int, analyst=None) -> dict:
    """Re-apply the CURRENT gates to a finished experiment's stored results.

    Nothing is re-backtested and the sealed test window is not opened again, so this
    adds no deflation trial. The current gates must be at least as strict as the ones the
    experiment was first judged under: loosening a threshold after seeing a result and
    re-judging it is choosing the bar to fit the score. The first judgement is kept.
    """
    e = db.q1("SELECT * FROM experiments WHERE id=?", (eid,))
    if not e or e["status"] not in ("failed", "passed") or not e["test"]:
        journal.write("system", f"Experiment {eid} cannot be re-judged", "Only finished experiments with stored test "
                      "results can be re-judged.", author="system")
        return {"status": "not_found"}
    if db.q1("SELECT 1 FROM strategies WHERE experiment_id=? AND probe=0", (eid,)):
        journal.write("system", f"Experiment {eid} already has a strategy", "Nothing to re-judge.", author="system")
        return {"status": "exists"}
    h = dict(db.q1("SELECT * FROM hypotheses WHERE id=?", (e["hypothesis_id"],)))
    res = {k: db.loads(e[k]) for k in ("windows", "chosen_params", "train", "validation", "test", "challenge", "leakage")}
    res["strategy_kind"] = e["kind"]
    spec, venue = db.loads(e["spec"]), e["venue"]
    gres = G.evaluate(res, settings.get("gates"))
    first = G.first_judgement(db.loads(e["gates"], None))
    loosened = G.loosened_since(first, gres)
    if loosened:
        detail = "; ".join(f"{x['name']} {x['op']} {x['now']} (was {x['was']})" for x in loosened)
        journal.write("system", f"Experiment {eid} not re-judged: gates are looser than when it ran",
                      "Re-judging a known result under looser thresholds picks the bar after seeing the score, so "
                      f"it is refused. Looser now: {detail}. Restore those thresholds to re-judge, or start a manual "
                      "probe to collect forward evidence instead.",
                      refs={"experiment_id": eid}, author="rules")
        return {"status": "refused_looser", "experiment_id": eid, "loosened": loosened}
    if first and first.get("checks"):
        gres["first_judged"] = {k: first[k] for k in ("passed", "checks", "failed") if k in first}
    db.ex("UPDATE experiments SET gates=?, status=? WHERE id=?",
          (db.dumps(gres), "passed" if gres["passed"] else "failed", eid))
    journal.write("test", f"Re-judged experiment {eid} under current gates ({'passed' if gres['passed'] else 'failed'})",
                  "Stored test results re-checked against the operator's current thresholds; no new backtest, "
                  "no new holdout look." + (f" Failed: {', '.join(gres['failed'])}." if gres["failed"] else ""),
                  refs={"experiment_id": eid, "hypothesis_id": h["id"]}, author="rules")
    if not gres["passed"]:
        return {"status": "done", "experiment_id": eid, "decision": "reject"}
    from ..trading.live import retire_strategy
    for p in db.q("SELECT * FROM strategies WHERE experiment_id=? AND probe=1 AND status IN ('active','incubating')", (eid,)):
        retire_strategy(dict(p), f"Promoted experiment {eid} from probe to full strategy.", origin="operator")
    prop = {k: h[k] for k in ("title", "rationale", "prediction", "falsification")}
    obs = record_observation(venue)
    return _decide(venue, eid, h["id"], prop, spec, res, gres, obs, settings.get("benchmarks")[venue],
                   settings.get("research"), analyst or get_analyst())


_FILLER = {"the", "a", "an", "is", "are", "was", "were", "be", "on", "in", "of", "to", "and", "or", "for", "with",
           "this", "that", "it", "its", "at", "as", "by", "so", "than", "currently", "still", "now", "has", "have"}


def _claim_words(s: str) -> set[str]:
    import re
    return {w.rstrip("s") for w in re.findall(r"[a-z0-9]+(?:\.[0-9]+)?", s.lower()) if w not in _FILLER}


def same_claim(a: str, b: str) -> bool:
    """Two phrasings of one claim share most of their content words (Jaccard >= 0.5)."""
    wa, wb = _claim_words(a), _claim_words(b)
    return bool(wa and wb) and len(wa & wb) / len(wa | wb) >= 0.5


def _save_beliefs(beliefs: list, venue: str, author: str, call_id) -> None:
    valid_types = {"experiment": "experiments", "strategy": "strategies", "order": "orders", "fill": "fills",
                   "observation": "observations", "hypothesis": "hypotheses", "decision": "decisions"}
    for b in beliefs or []:
        ev = []
        for ref in b.get("evidence", []):
            typ, _, rid = str(ref).partition(":")
            if typ in valid_types and rid.isdigit() and db.q1(f"SELECT 1 FROM {valid_types[typ]} WHERE id=?", (int(rid),)):
                ev.append(f"{typ}:{rid}")
        if not ev:
            continue  # beliefs without verifiable evidence are not stored
        for old in db.q("SELECT id, statement FROM beliefs WHERE status='active' AND venue=?", (venue,)):
            if same_claim(old["statement"], b["statement"]):
                db.ex("UPDATE beliefs SET status='superseded' WHERE id=?", (old["id"],))
        db.ex("INSERT INTO beliefs(ts, venue, statement, confidence, evidence, status, author, llm_call_id) "
              "VALUES(?,?,?,?,?,?,?,?)", (clock.now(), venue, b["statement"], b["confidence"], db.dumps(ev), "active",
                                          author, call_id))
    # keep the belief board short: newest 8 per venue stay active
    rows = db.q("SELECT id FROM beliefs WHERE status='active' AND venue=? ORDER BY id DESC", (venue,))
    for r in rows[8:]:
        db.ex("UPDATE beliefs SET status='superseded' WHERE id=?", (r["id"],))


def create_strategy(hid, eid, prop, spec, res, dec, author, call_id, probe: bool = False) -> int:
    lim = settings.get("risk")
    rs = settings.get("research")
    eq = ledger.equity_breakdown()["equity"]
    req_pct = max(0.0, float(dec.get("allocation_pct") or 0))
    approved_pct = min(req_pct, lim["max_strategy_pct"])
    approved = approved_pct * eq
    from ..trading.live import allocation_room
    frac = 1.0 if probe else rs["incubation_fraction"]  # probes are already small: no incubation haircut
    start_alloc = min(approved * frac, allocation_room(eq))  # never beyond total-allocation room
    if start_alloc < lim["min_order_usd"]:
        start_alloc = 0.0  # waits as 'incubating' until room frees up (live.activate_waiting)
    t = res["test"]
    v = 1 + db.q1("SELECT COUNT(*) n FROM strategies WHERE name=?", (spec["name"],))["n"]
    fixed = dict(spec, fixed_params=res["chosen_params"])
    expectations = {"test": t, "validation": res["validation"], "windows": res["windows"],
                    "bars_per_year": None, "chosen_params": res["chosen_params"]}
    inval = dec.get("invalidation") or {}
    now = clock.now()
    sid = db.ex("INSERT INTO strategies(hypothesis_id, experiment_id, name, version, venue, spec, spec_hash, status, "
                "approved_allocation, allocation, thesis, exit_plan, expectations, invalidation, created_at, activated_at, "
                "probe) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (hid, eid, spec["name"], v, spec["venue"], db.dumps(fixed), spec_hash(fixed),
                 "active" if start_alloc > 0 else "incubating", approved, start_alloc, dec.get("thesis") or prop["rationale"],
                 dec.get("exit_plan") or "", db.dumps(expectations), db.dumps(inval), now, now, int(probe)))
    from ..marketdata import freshness
    db.ex("UPDATE strategies SET last_signal_bar=? WHERE id=?", (freshness.expected_last_bar(spec["venue"], now), sid))
    note = ""
    if approved < req_pct * eq - 1:
        note = f" Requested {req_pct:.1%} of equity; operator limits capped approval at ${approved:,.0f}."
    full = approved * frac
    kind = "probe" if probe else "strategy"
    if start_alloc <= 0:
        title = f"{kind.capitalize()} approved, waiting for room: {spec['name']} v{v}"
        start = (f"No room under the total-allocation limit ({lim['max_total_allocation_pct']:.0%} of equity), so it "
                 "starts trading automatically when other strategies free up capital.")
    else:
        title = f"{'Probe started' if probe else 'New strategy live'}: {spec['name']} v{v}"
        start = (f"Trading starts with ${start_alloc:,.0f} on the next bar close"
                 + (f" (reduced from ${full:,.0f} by the total-allocation limit)." if start_alloc < full - 1 else
                    "." if probe else f" (incubation size, {rs['incubation_fraction']:.0%})."))
    journal.write("decision", title,
                  f"Approved ${approved:,.0f}. {start}{note}\nThesis: {dec.get('thesis')}\n"
                  f"Exit plan: {dec.get('exit_plan')}\nInvalidation: {inval}",
                  refs={"strategy_id": sid, "experiment_id": eid, **({"probe": True} if probe else {})},
                  author=author, llm_call_id=call_id)
    return sid
