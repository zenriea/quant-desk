import json
from types import SimpleNamespace

import numpy as np
import pytest

from conftest import put_bars


class FakeClient:
    def __init__(self, payloads, stop="end_turn"):
        self.payloads = list(payloads)
        self.stop = stop
        self.calls = []
        self.messages = SimpleNamespace(create=self._create)
        self.beta = SimpleNamespace(messages=SimpleNamespace(create=self._create))

    def _create(self, **kw):
        self.calls.append(kw)
        p = self.payloads.pop(0)
        return SimpleNamespace(content=[SimpleNamespace(type="thinking", thinking=""), SimpleNamespace(type="text", text=json.dumps(p))],
                               usage=SimpleNamespace(input_tokens=1000, output_tokens=500, cache_creation_input_tokens=0,
                                                     cache_read_input_tokens=0),
                               model=kw["model"], stop_reason=self.stop, stop_details=None)


def test_llm_call_records_cost_and_uses_structured_output(env):
    from quantdesk import db
    from quantdesk.agent.llm import LLM
    fc = FakeClient([{"answer": "x", "citations": [], "unknowns": None}])
    out, cid = LLM(fc).call("question", "sys", "user", {"type": "object"})
    assert out["answer"] == "x"
    kw = fc.calls[0]
    assert kw["output_config"]["format"]["type"] == "json_schema" and "thinking" not in kw
    row = db.q1("SELECT * FROM llm_calls WHERE id=?", (cid,))
    assert row["status"] == "ok" and row["cost_usd"] == pytest.approx((1000 * 0.10 + 500 * 0.50) / 1e6)  # claude-haiku-5-5 prices


def test_budget_blocks_call_before_it_is_made(env):
    from quantdesk import settings
    from quantdesk.agent.llm import LLM, BudgetExceeded
    settings.operator_update("budget", {"daily_usd": 0.001})
    fc = FakeClient([{}])
    with pytest.raises(BudgetExceeded):
        LLM(fc).call("hypothesis", "sys", "user", {"type": "object"})
    assert fc.calls == []


def test_refusal_and_truncation_are_errors(env):
    from quantdesk.agent.llm import LLM, LLMError
    for stop in ("refusal", "max_tokens"):
        with pytest.raises(LLMError):
            LLM(FakeClient([{}], stop=stop)).call("x", "s", "u", {"type": "object"})


def _seed_nasdaq():
    rng = np.random.default_rng(3)
    from quantdesk import settings
    for s in settings.get("universe")["NASDAQ"]:
        put_bars(s, list(100 * np.exp(np.cumsum(rng.normal(0.0004, 0.015, 900)))))


def test_research_cycle_records_in_order_and_enforces_gates(env):
    from quantdesk import db
    from quantdesk.agent.analyst import LLMAnalyst
    from quantdesk.agent.llm import LLM
    from quantdesk.agent.research_loop import research_cycle
    _seed_nasdaq()
    hyp = {"decision": "investigate", "holding_period_bars": 26, "edge_bps_per_trade": 500, "lookbacks_in_time": "26 bars = 1 session", "why": "testing", "title": "Trend", "rationale": "r", "prediction": "p",
           "falsification": "f", "parent_experiment_id": None,
           "spec": {"name": "trend", "universe": ["AAPL", "MSFT"], "kind": "dsl", "entry": "close > sma(close, n)",
                    "exit": "close < sma(close, n)", "code": None, "rank_by": None, "top_k": None,
                    "sizing": {"method": "fixed", "weight": 0.2, "max_weight": 0.2, "target_vol": 0.15, "vol_lookback": 20},
                    "stop_loss_pct": None, "take_profit_pct": None, "max_hold_bars": None,
                    "params": [{"name": "n", "values": [20, 50]}]}}
    # the model tries to allocate regardless of the evidence
    dec = {"decision": "allocate", "journal": "I like it", "lessons": ["l1"], "allocation_pct": 0.9, "thesis": "t",
           "exit_plan": "e", "invalidation": {"max_live_drawdown_pct": 0.1, "review_after_days": 7, "min_trades_before_judging": 3,
                                             "conditions": "c"}, "revision_idea": None,
           "beliefs": [{"statement": "b", "confidence": "low", "evidence": ["experiment:1", "experiment:999"]}]}
    fc = FakeClient([hyp, dec])
    res = research_cycle("NASDAQ", analyst=LLMAnalyst(LLM(fc)))
    e = db.q1("SELECT * FROM experiments")
    h = db.q1("SELECT * FROM hypotheses")
    assert h["created_at"] <= e["created_at"] <= e["frozen_at"] <= e["completed_at"]
    gates = json.loads(e["gates"])
    if not gates["passed"]:
        # no full allocation; a small probe may start if the sealed test was profitable
        assert res["decision"] == "reject" and db.q1("SELECT COUNT(*) n FROM strategies WHERE probe=0")["n"] == 0
        missed = len(gates["failed"])
        title = db.q1("SELECT title FROM journal WHERE kind='decision' ORDER BY id LIMIT 1")["title"]
        assert title == f"Not promoted (missed {missed} gate{'s' if missed != 1 else ''}): Trend"
        test = json.loads(e["test"])
        assert bool(res.get("probe_strategy_id")) == (test["total_return"] > 0 and test["n_trades"] >= 8)
    else:
        s = db.q1("SELECT * FROM strategies")
        assert s["approved_allocation"] <= 0.25 * 100000 + 1   # capped by operator limit, not 90%
        assert s["allocation"] == pytest.approx(s["approved_allocation"] * 0.5)
    # the decision prompt was built only after the test, and the hypothesis prompt saw blinded data
    hyp_prompt = fc.calls[0]["messages"][0]["content"]
    assert '"blinded": true' in hyp_prompt
    b = db.q("SELECT evidence FROM beliefs")
    assert all("999" not in r["evidence"] for r in b)         # unverifiable evidence dropped
    kinds = [r["kind"] for r in db.q("SELECT kind FROM journal ORDER BY id")]
    assert kinds.index("hypothesis") < kinds.index("test") < kinds.index("decision")


def test_rules_analyst_full_cycle_and_qa(env):
    from quantdesk import db
    from quantdesk.agent.qa import ask
    from quantdesk.agent.research_loop import research_cycle
    _seed_nasdaq()
    r = research_cycle("NASDAQ")
    assert r["status"] == "done"
    a = ask("What are you researching right now?")
    assert a["author"] == "rules" and a["answer"]
    assert all(c.startswith(("experiment:", "journal:", "order:", "strategy:")) for c in a["citations"])


def test_rejudge_applies_current_gates_without_new_test(env, monkeypatch):
    from quantdesk import db
    from quantdesk.agent import research_loop
    from quantdesk.agent.research_loop import rejudge_experiment, research_cycle
    _seed_nasdaq()
    research_cycle("NASDAQ")
    e = db.q1("SELECT id, test FROM experiments")
    n_exp = db.q1("SELECT COUNT(*) n FROM experiments")["n"]
    # Same thresholds as when it ran (a looser re-judge is refused); the outcome is stubbed to a pass
    # so the re-judge goes on to a decision.
    first = db.loads(db.q1("SELECT gates FROM experiments WHERE id=?", (e["id"],))["gates"])
    passing = [dict(c, **{"pass": True}) for c in first["checks"]]
    monkeypatch.setattr(research_loop.G, "evaluate", lambda res, g: {"passed": True, "checks": passing, "failed": []})
    db.ex("DELETE FROM strategies")
    out = rejudge_experiment(e["id"])
    assert out["status"] == "done"
    row = db.q1("SELECT status, test FROM experiments WHERE id=?", (e["id"],))
    assert row["status"] == "passed" and row["test"] == e["test"]   # stored results untouched
    assert db.q1("SELECT COUNT(*) n FROM experiments")["n"] == n_exp   # no new trial
    assert rejudge_experiment(999)["status"] == "not_found"


def test_no_idea_is_recorded_as_a_decision(env):
    from quantdesk import db
    from quantdesk.agent.analyst import LLMAnalyst
    from quantdesk.agent.llm import LLM
    from quantdesk.agent.research_loop import research_cycle
    _seed_nasdaq()
    noidea = {"decision": "no_idea", "holding_period_bars": None, "edge_bps_per_trade": None, "lookbacks_in_time": "", "why": "Nothing compelling: dispersion is low and prior trend tests failed.",
              "title": "", "rationale": "", "prediction": "", "falsification": "", "parent_experiment_id": None,
              "spec": None}
    research_cycle("NASDAQ", analyst=LLMAnalyst(LLM(FakeClient([noidea]))))
    assert db.q1("SELECT kind FROM decisions")["kind"] == "no_trade"
    assert db.q1("SELECT COUNT(*) n FROM experiments")["n"] == 0


def test_hypothesis_prompt_shows_costs_and_gates(env):
    from quantdesk.agent.analyst import LLMAnalyst
    from quantdesk.agent.llm import LLM
    from quantdesk.agent.research_loop import research_cycle
    _seed_nasdaq()
    noidea = {"decision": "no_idea", "holding_period_bars": None, "edge_bps_per_trade": None, "lookbacks_in_time": "", "why": "w", "title": "", "rationale": "", "prediction": "", "falsification": "",
              "parent_experiment_id": None, "spec": None}
    fc = FakeClient([noidea])
    research_cycle("NASDAQ", analyst=LLMAnalyst(LLM(fc)))
    prompt = fc.calls[0]["messages"][0]["content"]
    ctx = json.loads(prompt.split("CONTEXT (JSON):\n", 1)[1])
    assert ctx["trading_costs"]["approx_round_trip_bps"] == pytest.approx(6.0)  # 2 x (2 spread + 1 slippage)
    assert ctx["acceptance_gates"]["min_test_trades"] == 8
    assert "round trip" in prompt


def test_anti_whipsaw_examples_are_valid_rules():
    from quantdesk.agent import prompts
    from quantdesk.research import dsl
    assert "whipsaw" in prompts.hypothesis_prompt({})
    assert "several symbols" in prompts.hypothesis_prompt({})
    for expr in prompts.ANTI_WHIPSAW_EXAMPLES:
        dsl.validate(expr, {"n": 336})
