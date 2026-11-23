"""Deterministic acceptance gates. Thresholds are operator settings; the agent cannot
change them, and no allocation is possible unless every gate passes."""
from __future__ import annotations


def _g(name, value, op, threshold, why):
    if value is None:
        ok = False
    elif op == ">=":
        ok = value >= threshold
    elif op == ">":
        ok = value > threshold
    else:
        ok = value <= threshold
    return {"name": name, "value": value, "op": op, "threshold": threshold, "pass": bool(ok), "why": why}


def _top5(t: dict, threshold) -> dict:
    """Sealed-test profit share of the 5 best trades. Pre-test concentration stays in the record."""
    chk = _g("top5_concentration", t.get("top5_concentration"), "<=", threshold,
             "Sealed-test profits should not hinge on a handful of trades.")
    if chk["value"] is None and not (t.get("total_return") or 0) > 0:
        chk.update({"pass": True, "why": "No positive sealed-test profit to concentrate, so this check does not apply."})
    return chk


def universe_excess(t: dict) -> float | None:
    """Sealed-test return minus an exposure-matched equal-weight buy-and-hold of the same universe.

    The default universe is today's survivors, which inflates every long backtest on it. Holding
    the same names passively is inflated just as much, so this difference strips most of that
    survivorship drift and leaves the timing/selection the rules add."""
    bench = (t.get("benchmark_universe") or {}).get("total_return")
    ret = t.get("total_return")
    if bench is None or ret is None:
        return None
    return round(ret - (t.get("exposure") or 0.0) * bench, 5)


def evaluate(result: dict, g: dict) -> dict:
    t, v, c = result["test"], result["validation"], result["challenge"]
    checks = [
        _g("validation_sharpe", v.get("sharpe"), ">=", g["min_val_sharpe"],
           "Confirms the train-selected parameters on data not used for selection."),
        _g("test_sharpe", t.get("sharpe"), ">=", g["min_test_sharpe"],
           "Net-of-cost risk-adjusted return on the sealed test window."),
        _g("test_trades", t.get("n_trades"), ">=", g["min_test_trades"],
           "Too few trades make any test statistic meaningless."),
        _g("test_return_cost_x2", c["test_cost_x2"].get("total_return"), ">", g["min_cost_stress_return"],
           "Must survive doubled fees, spread and impact."),
        _g("test_sharpe_delay_1bar", c["test_delay_1bar"].get("sharpe"), ">", g["min_delay_stress_sharpe"],
           "Must survive executing one bar later than paper trading can."),
        _g("neighbor_positive_frac", c.get("neighbor_positive_frac"), ">=", g["min_neighbor_positive_frac"],
           "Nearby parameter values should also work (not a lucky point)."),
        _g("subperiod_positive_frac", c.get("subperiod_positive_frac"), ">=", g["min_subperiod_positive_frac"],
           "Pre-test performance should not come from a single period."),
        _top5(t, g["max_top5_concentration"]),
        _g("deflated_psr", t.get("deflated_psr"), ">=", g["min_deflated_psr"],
           "PSR score against the expected best of N zero-skill trials, under sampling assumptions; not proof of an edge."),
    ]
    if g.get("require_causal_dsl"):
        checks.append(_g("causal_dsl", result.get("strategy_kind") == "dsl", ">=", True,
                         "Strict screening excludes Python: sampled prefix checks cannot prove causality."))
    if g.get("require_universe_excess"):
        checks.append(_g("universe_excess_return", universe_excess(t), ">", 0.0,
                         "Must beat holding its own universe at the same average exposure: survivorship "
                         "inflates both, so only the rules' added value counts."))
    passed = all(x["pass"] for x in checks)
    return {"passed": passed, "checks": checks,
            "failed": [x["name"] for x in checks if not x["pass"]]}
