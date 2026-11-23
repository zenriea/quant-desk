"""Gates are judged once at the thresholds in force; a looser re-judge of a known result is refused,
and proposals must expect an edge that survives the doubled-cost stress."""
import json

import pytest

from quantdesk.config import GATE_PRESETS
from quantdesk.research import gates as G
from quantdesk.research.strategy import SpecError


def _result(dpsr=0.12, top5=0.4):
    return {"test": {"sharpe": 1.2, "n_trades": 60, "deflated_psr": dpsr, "top5_concentration": top5,
                     "total_return": 0.06},
            "validation": {"sharpe": 0.8},
            "challenge": {"test_cost_x2": {"total_return": 0.03}, "test_delay_1bar": {"sharpe": 0.6},
                          "neighbor_positive_frac": 0.8, "subperiod_positive_frac": 0.75}}


def _gates(**patch):
    g = dict(GATE_PRESETS["exploratory"])
    g.update(patch)
    return g


def test_loosened_since_flags_only_looser_thresholds():
    first = G.evaluate(_result(), _gates(min_deflated_psr=0.25, max_top5_concentration=1.0))
    looser = G.evaluate(_result(), _gates(min_deflated_psr=0.05, max_top5_concentration=2.5))
    names = {x["name"] for x in G.loosened_since(first, looser)}
    assert names == {"deflated_psr", "top5_concentration"}
    stricter = G.evaluate(_result(), _gates(min_deflated_psr=0.5, max_top5_concentration=0.5))
    assert G.loosened_since(first, stricter) == []
    assert G.loosened_since(None, looser) == []  # nothing recorded: nothing to compare


def test_first_judgement_survives_a_rejudge_record():
    first = {"passed": False, "checks": [{"name": "deflated_psr", "op": ">=", "threshold": 0.25}], "failed": ["x"]}
    assert G.first_judgement(first) is first
    assert G.first_judgement({"passed": True, "checks": [], "first_judged": first}) is first


def _experiment_judged_at(dpsr_threshold):
    from quantdesk import clock, db
    now = clock.now()
    res = _result()
    gres = G.evaluate(res, _gates(min_deflated_psr=dpsr_threshold))
    hid = db.ex("INSERT INTO hypotheses(created_at, venue, title, rationale, prediction, falsification, status, "
                "author, updated_at) VALUES(?,?,?,?,?,?,?,?,?)",
                (now, "NASDAQ", "Low-vol rotation", "r", "p", "f", "rejected", "llm", now))
    spec = {"name": "lowvol", "venue": "NASDAQ", "universe": ["AAPL", "MSFT"], "kind": "dsl",
            "entry": "close > sma(close, n)", "exit": "close < sma(close, n)", "params": {"n": [26]},
            "sizing": {"method": "fixed", "weight": 0.2}}
    return db.ex("INSERT INTO experiments(hypothesis_id, created_at, venue, kind, spec, spec_hash, status, windows, "
                 "chosen_params, train, validation, test, challenge, gates, leakage, completed_at) "
                 "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                 (hid, now, "NASDAQ", "dsl", json.dumps(spec), "h", "passed" if gres["passed"] else "failed", "{}",
                  json.dumps({"n": 26}), "{}", json.dumps(res["validation"]), json.dumps(res["test"]),
                  json.dumps(res["challenge"]), json.dumps(gres), "{}", now))


def test_rejudge_under_looser_gates_is_refused_and_keeps_the_record(env):
    from quantdesk import db, settings
    from quantdesk.agent.research_loop import rejudge_experiment
    eid = _experiment_judged_at(0.25)          # deflated PSR 0.12 misses 0.25
    before = db.q1("SELECT status, gates FROM experiments WHERE id=?", (eid,))
    assert before["status"] == "failed"
    settings.operator_update("gates", {"min_deflated_psr": 0.05})
    out = rejudge_experiment(eid)
    assert out["status"] == "refused_looser" and out["loosened"][0]["name"] == "deflated_psr"
    after = db.q1("SELECT status, gates FROM experiments WHERE id=?", (eid,))
    assert dict(after) == dict(before)          # first judgement untouched
    assert not db.q1("SELECT 1 FROM strategies WHERE experiment_id=?", (eid,))
    assert db.q1("SELECT 1 FROM journal WHERE title LIKE ?", (f"Experiment {eid} not re-judged%",))


def test_rejudge_under_equal_or_stricter_gates_keeps_the_first_judgement(env):
    from quantdesk import db
    from quantdesk.agent.research_loop import rejudge_experiment
    eid = _experiment_judged_at(0.25)
    out = rejudge_experiment(eid)               # same gates as when it ran
    assert out["status"] == "done" and out["decision"] == "reject"
    g = json.loads(db.q1("SELECT gates FROM experiments WHERE id=?", (eid,))["gates"])
    assert g["first_judged"]["failed"] == ["deflated_psr"]


def test_edge_must_cover_twice_the_round_trip_cost(env):
    from quantdesk.agent.research_loop import EDGE_COST_MULTIPLE, validate_trade_economics
    assert EDGE_COST_MULTIPLE == 2.0
    spec = {"venue": "CRYPTO", "entry": "close > sma(close, n)", "exit": "close < sma(close, n)",
            "params": {"n": [96]}}
    prop = {"holding_period_bars": 192, "edge_bps_per_trade": 80, "lookbacks_in_time": "96 bars = 1 day"}
    with pytest.raises(SpecError, match="2x the round-trip cost"):
        validate_trade_economics(prop, spec, "CRYPTO", 57.0)
    validate_trade_economics(dict(prop, edge_bps_per_trade=120), spec, "CRYPTO", 57.0)
