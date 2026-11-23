"""Research proposals must be capable of running the mandatory robustness check."""
import copy

import pytest

from quantdesk.config import DEFAULT_SETTINGS, GATE_PRESETS
from quantdesk.research import experiment as E
from quantdesk.research.strategy import SpecError, validate_spec


def _spec(**patch):
    spec = {
        "name": "trend", "venue": "NASDAQ", "universe": ["AAPL"], "kind": "dsl",
        "entry": "close > sma(close, n)", "exit": "close < sma(close, n)",
        "params": {"n": [26]}, "sizing": {"method": "fixed", "weight": 0.2},
    }
    spec.update(patch)
    return spec


@pytest.mark.parametrize("kind", ["dsl", "code"])
def test_parameterless_specs_remain_valid_but_are_not_admissible_research(kind):
    spec = _spec(kind=kind, params={}, entry="close > sma(close, 26)", exit="close < sma(close, 26)")
    if kind == "code":
        spec["code"] = "def signals(o, h, l, c, v, p):\n    return c > o, c < o\n"
    validate_spec(spec, DEFAULT_SETTINGS["universe"])
    with pytest.raises(SpecError, match="neighbor"):
        E.validate_research_spec(spec, DEFAULT_SETTINGS["universe"])


@pytest.mark.parametrize("params", [{"unused": [26]}, {"n": [26], "unused": [1]}])
def test_unused_dsl_parameter_cannot_stand_in_for_a_robustness_parameter(params):
    spec = _spec(params=params)
    if "n" not in params:
        spec.update(entry="close > sma(close, 26)", exit="close < sma(close, 26)")
    with pytest.raises(SpecError, match="unused"):
        E.validate_research_spec(spec, DEFAULT_SETTINGS["universe"])


@pytest.mark.parametrize("spec", [
    _spec(),  # a single train candidate is sufficient; no extra optimisation is required
    _spec(params={"n": [26, 52]}),
    _spec(entry="close > 0", exit="close < 0", rank_by="ret(close, n)", top_k=1),
    _spec(kind="code", code="def signals(o, h, l, c, v, p):\n    return c > p['n'], c < p['n']\n"),
])
def test_used_parameters_with_valid_neighbors_are_admitted(spec):
    original = copy.deepcopy(spec)
    assert E.validate_research_spec(spec, DEFAULT_SETTINGS["universe"]) == original
    assert spec == original  # admission must not silently tune or rewrite the rules


def test_every_train_candidate_needs_a_valid_neighbor():
    spec = _spec(entry="close > lag(close, n)", exit="close < 0", params={"n": [0, 26]})
    validate_spec(spec, DEFAULT_SETTINGS["universe"])  # zero-lag itself is legal DSL
    with pytest.raises(SpecError, match="neighbor"):
        E.validate_research_spec(spec, DEFAULT_SETTINGS["universe"])


def test_neighbor_validation_keeps_usable_boundary_neighbors():
    spec = _spec(params={"n": [4000]})
    # 5000 is out of bounds, but 3000 remains a valid perturbation.
    E.validate_research_spec(spec, DEFAULT_SETTINGS["universe"])


def test_unrepresentable_neighbor_is_a_repairable_spec_error():
    spec = _spec(entry="close < threshold", exit="close > threshold", params={"threshold": [1.7e308]})
    with pytest.raises(SpecError, match="neighbor"):
        E.validate_research_spec(spec, DEFAULT_SETTINGS["universe"])


@pytest.mark.parametrize("venue", ["NASDAQ", "CRYPTO"])
def test_all_demo_templates_have_admissible_robustness_parameters(venue):
    from quantdesk.agent.analyst import RulesAnalyst, TEMPLATES
    ctx = {"venue": venue, "approved_universe": DEFAULT_SETTINGS["universe"][venue],
           "benchmark": DEFAULT_SETTINGS["benchmarks"][venue], "research_memory": []}
    for template in TEMPLATES[venue]:
        proposal, _ = RulesAnalyst().propose(ctx)
        E.validate_research_spec(proposal["spec"], DEFAULT_SETTINGS["universe"])
        ctx["research_memory"].append({"template": template["key"]})


def _proposal(parameterized=False):
    return {
        "decision": "investigate", "holding_period_bars": 26, "edge_bps_per_trade": 500, "lookbacks_in_time": "26 bars = 1 session", "why": "testing", "title": "Trend", "rationale": "r",
        "prediction": "p", "falsification": "f", "parent_experiment_id": None,
        "spec": {
            "name": "trend", "universe": ["AAPL"], "kind": "dsl",
            "entry": "close > sma(close, n)" if parameterized else "close > sma(close, 26)",
            "exit": "close < sma(close, n)" if parameterized else "close < sma(close, 26)",
            "code": None, "rank_by": None, "top_k": None,
            "sizing": {"method": "fixed", "weight": 0.2, "max_weight": 0.2,
                       "target_vol": 0.15, "vol_lookback": 26},
            "stop_loss_pct": None, "take_profit_pct": None, "max_hold_bars": None,
            "params": [{"name": "n", "values": [26]}] if parameterized else [],
        },
    }


def _no_idea():
    return {"decision": "no_idea", "holding_period_bars": None, "edge_bps_per_trade": None, "lookbacks_in_time": "", "why": "No meaningful robustness parameter for this idea.",
            "title": "", "rationale": "", "prediction": "", "falsification": "",
            "parent_experiment_id": None, "spec": None}


def _client(payloads):
    from quantdesk.agent.analyst import LLMAnalyst
    from quantdesk.agent.llm import LLM
    from test_agent import FakeClient
    client = FakeClient(payloads)
    return client, LLMAnalyst(LLM(client))


def _seed():
    from test_agent import _seed_nasdaq
    _seed_nasdaq()


def _no_experiments():
    from quantdesk import db
    for table in ("hypotheses", "experiments", "strategies", "orders", "fills"):
        assert db.q1(f"SELECT COUNT(*) n FROM {table}")["n"] == 0


def test_parameterless_proposal_is_repaired_before_an_experiment(env):
    import json
    from quantdesk import db
    from quantdesk import settings
    from quantdesk.agent.research_loop import research_cycle
    _seed()
    # A rejection (and no probe) avoids making the test depend on the generated market's performance.
    settings.operator_update("probe", {"enabled": False})
    decision = {"decision": "reject", "journal": "Not allocating.", "lessons": [], "allocation_pct": 0,
                "thesis": "", "exit_plan": "",
                "invalidation": {"max_live_drawdown_pct": 0.1, "review_after_days": 7,
                                 "min_trades_before_judging": 3, "conditions": "c"},
                "revision_idea": None, "beliefs": []}
    client, analyst = _client([_proposal(), _proposal(parameterized=True), decision])
    result = research_cycle("NASDAQ", analyst=analyst)
    assert result["status"] == "done" and result["decision"] == "reject"
    assert len(client.calls) == 3
    repair_prompt = client.calls[1]["messages"][0]["content"]
    assert "neighbor" in repair_prompt and "validation_error" in repair_prompt
    experiment = db.q1("SELECT * FROM experiments")
    assert json.loads(experiment["spec"])["params"] == {"n": [26]}
    assert json.loads(experiment["chosen_params"]) == {"n": 26}
    assert json.loads(experiment["challenge"])["neighbors"]
    assert db.q1("SELECT COUNT(*) n FROM experiments")["n"] == 1
    assert db.q1("SELECT COUNT(*) n FROM strategies")["n"] == 0


def test_unrepaired_parameterless_proposal_never_touches_the_holdout(env, monkeypatch):
    from quantdesk import db
    from quantdesk.agent import research_loop
    _seed()
    client, analyst = _client([_proposal(), _proposal()])
    monkeypatch.setattr(research_loop, "run_experiment",
                        lambda *a, **kw: pytest.fail("inadmissible spec reached the backtester"))
    result = research_loop.research_cycle("NASDAQ", analyst=analyst)
    assert result["status"] == "invalid_spec" and "neighbor" in result["error"]
    assert len(client.calls) == 2  # bounded repair, not an unbounded paid retry loop
    _no_experiments()
    note = db.q1("SELECT body FROM journal WHERE title LIKE 'Discarded an invalid strategy spec%'")
    assert "neighbor" in note["body"] and "Nothing was tested" in note["body"]


def test_repair_can_decline_the_idea_without_crashing_or_testing(env, monkeypatch):
    from quantdesk import db
    from quantdesk.agent import research_loop
    _seed()
    client, analyst = _client([_proposal(), _no_idea()])
    monkeypatch.setattr(research_loop, "run_experiment",
                        lambda *a, **kw: pytest.fail("declined repair reached the backtester"))
    result = research_loop.research_cycle("NASDAQ", analyst=analyst)
    assert result["status"] == "no_idea" and len(client.calls) == 2
    _no_experiments()
    decision = db.q1("SELECT kind, rationale FROM decisions")
    assert decision["kind"] == "no_trade" and decision["rationale"] == _no_idea()["why"]


@pytest.mark.parametrize("failure", ["budget", "provider"])
def test_unavailable_repair_still_prevents_testing(env, monkeypatch, failure):
    from quantdesk.agent import research_loop
    from quantdesk.agent.llm import BudgetExceeded, LLMError
    _seed()
    client, analyst = _client([_proposal()])

    def unavailable(*args):
        raise (BudgetExceeded if failure == "budget" else LLMError)("repair unavailable")

    monkeypatch.setattr(analyst, "repair", unavailable)
    monkeypatch.setattr(research_loop, "run_experiment",
                        lambda *a, **kw: pytest.fail("unrepaired spec reached the backtester"))
    result = research_loop.research_cycle("NASDAQ", analyst=analyst)
    assert result == {"status": "invalid_spec", "error": "repair unavailable"}
    assert len(client.calls) == 1
    _no_experiments()


@pytest.mark.parametrize("preset", ["exploratory", "strict"])
def test_missing_neighbor_evidence_still_fails_closed(preset):
    from quantdesk.research import gates
    result = {
        "strategy_kind": "dsl", "validation": {"sharpe": 1},
        "test": {"sharpe": 1, "n_trades": 100, "deflated_psr": 0.99, "total_return": 0.0, "exposure": 0.5,
                 "benchmark_universe": {"total_return": -0.1}},
        "challenge": {"test_cost_x2": {"total_return": 0.1}, "test_delay_1bar": {"sharpe": 1},
                      "neighbor_positive_frac": None, "subperiod_positive_frac": 1,
                      "top5_concentration_pre_test": 0.3},
    }
    assert gates.evaluate(result, GATE_PRESETS[preset])["failed"] == ["neighbor_positive_frac"]


def test_hypothesis_prompt_explains_single_value_robustness_parameters():
    from quantdesk.agent.prompts import hypothesis_prompt
    prompt = hypothesis_prompt({})
    assert "neighbor_positive_frac" in prompt
    assert "single" in prompt and "unused" in prompt
