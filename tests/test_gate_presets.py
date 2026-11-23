import pytest
import copy

from quantdesk.config import DEFAULT_SETTINGS, GATE_PRESETS
from quantdesk.research import gates


def _result(kind="dsl"):
    return {
        "strategy_kind": kind,
        "validation": {"sharpe": 1.0},
        "test": {"sharpe": 1.0, "n_trades": 50, "deflated_psr": 0.99, "total_return": 0.2, "exposure": 0.5,
                 "top5_concentration": 0.3, "benchmark_universe": {"total_return": 0.1}},
        "challenge": {
            "test_cost_x2": {"total_return": 0.05},
            "test_delay_1bar": {"sharpe": 0.5},
            "neighbor_positive_frac": 1.0, "subperiod_positive_frac": 1.0,
            "top5_concentration_pre_test": 0.3,
        },
    }


def test_default_is_explicit_exploratory_and_does_not_alias_presets():
    assert DEFAULT_SETTINGS["gates"] == GATE_PRESETS["exploratory"]
    assert DEFAULT_SETTINGS["gates"] is not GATE_PRESETS["exploratory"]


def test_strict_accepts_good_dsl_but_not_sample_checked_python_or_unknown_kind():
    assert gates.evaluate(_result(), GATE_PRESETS["strict"])["passed"]
    for kind in ("code", None):
        result = gates.evaluate(_result(kind), GATE_PRESETS["strict"])
        assert result["failed"] == ["causal_dsl"]
    assert gates.evaluate(_result("code"), GATE_PRESETS["exploratory"])["passed"]


def test_exploratory_admission_is_not_strict_validation():
    result = copy.deepcopy(_result())
    result["validation"]["sharpe"] = -0.2
    result["test"].update(sharpe=0.1, n_trades=8, deflated_psr=0.3)
    result["challenge"]["test_cost_x2"]["total_return"] = -0.01
    assert gates.evaluate(result, GATE_PRESETS["exploratory"])["passed"]
    strict = gates.evaluate(result, GATE_PRESETS["strict"])
    assert not strict["passed"]
    assert {"validation_sharpe", "test_trades", "deflated_psr", "test_return_cost_x2"} <= set(strict["failed"])


def _with_universe(test_return, exposure, universe_return):
    r = _result()
    r["test"].update(total_return=test_return, exposure=exposure,
                     benchmark_universe={"total_return": universe_return})
    return r


def test_universe_excess_strips_passive_survivor_drift():
    # 60 % invested, universe rose 20 %: passive drift is 12 %, so +10 % is -2 % excess.
    assert gates.universe_excess(_with_universe(0.10, 0.6, 0.20)["test"]) == pytest.approx(-0.02)
    assert gates.universe_excess({"total_return": 0.1}) is None


def test_strict_requires_beating_its_universe_exploratory_does_not():
    lagging = _with_universe(0.10, 0.6, 0.20)
    strict = gates.evaluate(lagging, GATE_PRESETS["strict"])
    assert strict["failed"] == ["universe_excess_return"]
    assert gates.evaluate(_with_universe(0.15, 0.6, 0.20), GATE_PRESETS["strict"])["passed"]
    assert gates.evaluate(lagging, GATE_PRESETS["exploratory"])["passed"]
    # a result without a universe benchmark cannot show the check passed
    no_bench = _result()
    del no_bench["test"]["benchmark_universe"]
    assert gates.evaluate(no_bench, GATE_PRESETS["strict"])["failed"] == ["universe_excess_return"]


def test_strict_settings_saved_before_the_universe_gate_stay_strict(env):
    from quantdesk import db, settings
    old_strict = {k: v for k, v in GATE_PRESETS["strict"].items() if k != "require_universe_excess"}
    db.ex("INSERT OR REPLACE INTO settings(key, value) VALUES('gates', ?)", (db.dumps(old_strict),))
    assert settings.get("gates") == GATE_PRESETS["strict"]
    custom = {**old_strict, "min_test_trades": 12}
    db.ex("INSERT OR REPLACE INTO settings(key, value) VALUES('gates', ?)", (db.dumps(custom),))
    assert settings.get("gates")["require_universe_excess"] is False
