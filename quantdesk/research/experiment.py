"""Experiment pipeline: train -> validation -> (freeze) -> sealed test, plus challenges.

Contamination controls
  * Parameters are chosen on TRAIN only; VALIDATION confirms; TEST is evaluated once,
    after the spec + chosen params are frozen and persisted (``on_freeze`` callback).
  * Each experiment records its test window. Later experiments whose test window overlaps
    are counted as re-use of that holdout, and the Deflated Sharpe benchmark rises with
    the number of trials.
  * The LLM never sees test results before it commits to a spec (research_loop.py).
"""
from __future__ import annotations

import ast
import datetime as dt
import math

import numpy as np

from ..marketdata import store
from ..marketdata.freshness import signal_usable_after
from ..venues import VENUES
from . import backtest as B
from . import dsl, metrics
from .strategy import SpecError, compute_signals, param_grid, spec_hash, validate_spec
from ..trading import costs as C


# Stored with every experiment from this version on (in its leakage record). Earlier experiments have no
# timing record: they filled at a whole-bar count (NASDAQ: the open two bars after the signal bar, up to a
# minute before that bar was published). Their stored results are kept as they ran and are not recomputed.
EXECUTION_TIMING = {"version": 2, "rule": "fill at the first bar open at or after signal bar close + data delay"}


class ExperimentError(RuntimeError):
    pass


def required_warmup(spec: dict, params_all: dict) -> int:
    nums = [30]
    for key in ("entry", "exit", "rank_by"):
        expr = spec.get(key)
        if not expr:
            continue
        for node in ast.walk(ast.parse(expr, mode="eval")):
            if isinstance(node, ast.Constant) and isinstance(node.value, (int, float)) and float(node.value).is_integer():
                if 1 < node.value <= dsl.MAX_WINDOW:
                    nums.append(int(node.value))
    for vals in (params_all or {}).values():
        for v in vals:
            if isinstance(v, (int, float)) and float(v).is_integer() and 1 < v <= dsl.MAX_WINDOW:
                nums.append(int(v))
    lb = int((spec.get("sizing") or {}).get("vol_lookback", 20))
    nums.append(lb + 2)
    return min(max(nums) + 25, dsl.MAX_WINDOW + 25)


def build_panel(venue: str, symbols: list[str], benchmark: str | None, end_ts: int | None = None) -> dict:
    tf = VENUES[venue].timeframe
    syms = list(dict.fromkeys(symbols + ([benchmark] if benchmark else [])))
    panel = store.load_panel(syms, tf, end=end_ts)
    panel["benchmark"] = benchmark
    return panel


def split_windows(n_bars: int, warmup: int, train_frac: float, val_frac: float) -> dict:
    usable = n_bars - warmup
    if usable < 120:
        raise ExperimentError(f"not enough history: {n_bars} bars, need warmup {warmup} + 120")
    a = warmup + int(usable * train_frac)
    b = a + int(usable * val_frac)
    return {"train": (warmup, a), "validation": (a, b), "test": (b, n_bars)}


def _ts(panel, i):
    i = min(max(i, 0), len(panel["ts"]) - 1)
    return int(panel["ts"][i])


def _curve(res: B.BTResult, max_points: int = 300) -> dict:
    eq = res.equity
    if eq.size == 0 or not np.isfinite(eq[0]) or eq[0] <= 0:
        return {"ts": [], "v": []}
    idx = np.linspace(0, eq.size - 1, min(max_points, eq.size)).astype(int)
    pts = [(int(res.ts[i]), round(float(eq[i] / eq[0]), 5)) for i in idx if np.isfinite(eq[i])]
    return {"ts": [p[0] for p in pts], "v": [p[1] for p in pts]}


def _bh_curve(panel, symbols, start, end, max_points=300) -> dict:
    cols = [panel["symbols"].index(s) for s in symbols if s in panel["symbols"]]
    if not cols or end <= start:
        return {"ts": [], "v": []}
    c = panel["close"][start:end, cols]
    first = np.array([c[np.isfinite(c[:, k]), k][0] if np.isfinite(c[:, k]).any() else np.nan
                      for k in range(len(cols))])
    with np.errstate(invalid="ignore", divide="ignore"):
        ratio = c / first
        valid = np.isfinite(ratio)
        count = valid.sum(axis=1)
        rel = np.where(count > 0, np.where(valid, ratio, 0.0).sum(axis=1) / count, np.nan)
    idx = np.linspace(0, rel.size - 1, min(max_points, rel.size)).astype(int)
    pts = [(int(panel["ts"][start + i]), round(float(rel[i]), 5)) for i in idx if np.isfinite(rel[i])]
    return {"ts": [p[0] for p in pts], "v": [p[1] for p in pts]}


def _perturb(params: dict) -> list[dict]:
    out = []
    for k, v in params.items():
        for mult in (0.75, 1.25):
            nv = v * mult
            if float(v).is_integer():
                nv = int(round(nv))
                if nv == v:
                    nv = v + (1 if mult > 1 else -1)
                if nv < 2:
                    continue
            p = dict(params)
            p[k] = nv
            out.append(p)
    return out


def validate_research_spec(spec: dict, allowed_symbols: dict[str, list[str]], max_combos: int = 12) -> dict:
    """Preflight the mandatory neighbor challenge before creating an experiment.

    Parameterless rules remain valid strategy specs, but cannot pass this research
    pipeline's neighbor gate. Ask the analyst to expose an existing rule constant as
    a parameter (a single candidate is enough), rather than inventing a passing score.
    This check reads no market data and does not rewrite the proposed rules.
    """
    validate_spec(spec, allowed_symbols, max_combos)
    params = spec.get("params") or {}
    if not params:
        raise SpecError("Research requires at least one rule parameter for the mandatory neighbor_positive_frac "
                        "check. Expose a meaningful existing lookback or threshold in params; a single candidate "
                        "at its original value is enough. Empty params generate no neighbors and cannot pass.")
    grid = param_grid(spec)
    exprs = [spec[key] for key in ("entry", "exit", "rank_by") if spec.get(key)] \
        if spec.get("kind", "dsl") == "dsl" else []
    if exprs:
        used = set().union(*(dsl.validate(expr, grid[0]) for expr in exprs))
        unused = sorted(set(params) - used)
        if unused:
            raise SpecError(f"Research params are unused in the DSL rules: {', '.join(unused)}. "
                            "Declare meaningful rule parameters, not dummy neighbor checks.")
    # Training can select any grid candidate. Check the same perturbations used by
    # the experiment, including DSL window bounds, without looking at test results.
    for candidate in grid:
        try:
            neighbors = _perturb(candidate)
        except (ValueError, OverflowError) as e:
            raise SpecError(f"Cannot generate neighbor perturbations for {candidate}: {e}") from None
        for neighbor in neighbors:
            try:
                for expr in exprs:
                    dsl.validate(expr, neighbor)
            except dsl.DSLError:
                continue
            break
        else:
            raise SpecError(f"No valid neighbor perturbations for train candidate {candidate}. "
                            "Use a meaningful rule parameter whose nearby values are valid.")
    return spec


def _slim(m: dict) -> dict:
    keys = ("total_return", "cagr", "ann_vol", "sharpe", "sortino", "max_drawdown", "n_trades",
            "win_rate", "avg_trade_return", "exposure", "cost_drag", "fees_paid", "slippage_paid",
            "top5_concentration", "n_bars")
    return {k: (round(m[k], 5) if isinstance(m.get(k), float) else m.get(k)) for k in keys if k in m}


def run_experiment(spec: dict, costs_cfg: dict, research_cfg: dict, benchmark: str,
                   prior_test_windows: list[tuple[int, int]], llm_cutoff: str,
                   on_freeze=None, end_ts: int | None = None, sandbox_runner=None,
                   llm_authored: bool = True, max_participation: float = 0.01) -> dict:
    venue = spec["venue"]
    v = VENUES[venue]
    panel = build_panel(venue, spec["universe"], benchmark, end_ts)
    T = len(panel["ts"])
    if T == 0:
        raise ExperimentError(f"no stored {v.timeframe} bars for {spec['universe']}")
    grid = param_grid(spec)
    warm = required_warmup(spec, spec.get("params") or {})
    W = split_windows(T, warm, research_cfg["train_frac"], research_cfg["val_frac"])
    liq = B.liquidity_arrays(panel, venue)
    usable_after = signal_usable_after(venue)  # fill at the first open paper trading could reach
    sig_cache: dict = {}

    def sig_for(p):
        key = tuple(sorted(p.items()))
        if key not in sig_cache:
            sig_cache[key] = compute_signals(spec, p, panel, sandbox_runner)
        return sig_cache[key]

    def bt(p, win, cost=costs_cfg, extra_delay=0):
        s, e = W[win] if isinstance(win, str) else win
        return B.run(spec, panel, sig_for(p), cost, s, e, delay=extra_delay, usable_after=usable_after, liquidity=liq,
                     max_participation=max_participation)

    # ---- 1. grid search on TRAIN only
    grid_results = []
    for p in grid:
        r = bt(p, "train")
        grid_results.append({"params": p, "sharpe": round(r.metrics["sharpe"], 4),
                             "total_return": round(r.metrics["total_return"], 4),
                             "n_trades": r.metrics["n_trades"]})
    eligible = [g for g in grid_results if g["n_trades"] >= 3] or grid_results
    best = max(eligible, key=lambda g: g["sharpe"])
    chosen = best["params"]
    train = bt(chosen, "train")
    # ---- 2. VALIDATION
    val = bt(chosen, "validation")
    val_cost2 = bt(chosen, "validation", C.scaled(costs_cfg, 2.0))
    val_delay = bt(chosen, "validation", extra_delay=1)
    # ---- 3. challenges on pre-test data (train+validation)
    pre = (W["train"][0], W["validation"][1])
    pre_run = bt(chosen, pre)
    neighbors = []
    for p in _perturb(chosen):
        try:
            r = bt(p, pre)
            neighbors.append({"params": p, "sharpe": round(r.metrics["sharpe"], 4),
                              "total_return": round(r.metrics["total_return"], 4)})
        except Exception as e:  # invalid window after perturbation
            neighbors.append({"params": p, "error": str(e)[:120]})
    ok_n = [n for n in neighbors if "sharpe" in n]
    subperiods = []
    span = pre[1] - pre[0]
    for k in range(4):
        s = pre[0] + span * k // 4
        e = pre[0] + span * (k + 1) // 4
        r = bt(chosen, (s, e))
        subperiods.append({"start": _ts(panel, s), "end": _ts(panel, e - 1),
                           "total_return": round(r.metrics["total_return"], 4),
                           "sharpe": round(r.metrics["sharpe"], 3), "n_trades": r.metrics["n_trades"]})
    windows = {k: {"start": _ts(panel, s), "end": _ts(panel, e - 1), "bars": e - s} for k, (s, e) in W.items()}
    # ---- 4. FREEZE before touching the test window
    frozen = {"spec_hash": spec_hash(spec), "chosen_params": chosen, "windows": windows,
              "data_end": _ts(panel, T - 1), "max_participation": max_participation,
              "execution_timing": EXECUTION_TIMING | {"usable_after_close_seconds": usable_after}}
    if on_freeze:
        on_freeze(frozen)
    # ---- 5. sealed TEST (evaluated once)
    test = bt(chosen, "test")
    test_cost2 = bt(chosen, "test", C.scaled(costs_cfg, 2.0))
    test_delay = bt(chosen, "test", extra_delay=1)
    s, e = W["test"]
    bench_univ = B.buy_and_hold(panel, spec["universe"], s, e, costs_cfg, venue)
    bench_venue = B.buy_and_hold(panel, [benchmark], s, e, costs_cfg, venue) if benchmark in panel["symbols"] else {}
    # holdout reuse + deflated Sharpe
    t_start, t_end = windows["test"]["start"], windows["test"]["end"]
    reuse = sum(1 for (a, b) in prior_test_windows if a <= t_end and b >= t_start)
    n_trials = reuse + 1
    m = test.metrics
    sr_star = metrics.deflated_sr_benchmark(n_trials, max(m["n_bars"], 3))
    dpsr = metrics.psr(m.get("per_bar_sharpe", 0.0), m["n_bars"], m.get("skew", 0.0), m.get("kurt", 3.0), sr_star)
    raw_psr = metrics.psr(m.get("per_bar_sharpe", 0.0), m["n_bars"], m.get("skew", 0.0), m.get("kurt", 3.0), 0.0)
    # leakage disclosure
    cutoff = dt.datetime.fromisoformat(llm_cutoff).replace(tzinfo=dt.timezone.utc).timestamp()
    test_len = max(t_end - t_start, 1)
    before_cutoff = max(0.0, min(t_end, cutoff) - t_start) / test_len
    leakage = {
        "llm_knowledge_cutoff": llm_cutoff,
        "llm_authored": llm_authored,
        "test_fraction_before_llm_cutoff": round(before_cutoff, 3) if llm_authored else 0.0,
        "holdout_reuse_count": reuse,
        "n_trials_for_deflation": n_trials,
        "notes": _leak_notes(venue, llm_authored, before_cutoff, reuse),
    }
    out = {
        "strategy_kind": spec.get("kind", "dsl"),
        "venue": venue, "spec_hash": frozen["spec_hash"], "execution_timing": frozen["execution_timing"], "windows": windows, "chosen_params": chosen,
        "grid_results": grid_results,
        "train": _slim(train.metrics),
        "validation": _slim(val.metrics),
        "test": {**_slim(test.metrics), "psr": round(raw_psr, 4), "deflated_psr": round(dpsr, 4),
                 "sr_star_annualised": round(sr_star * math.sqrt(v.periods_per_year), 3),
                 "benchmark_universe": _slim(bench_univ) if bench_univ else None,
                 "benchmark_venue": _slim(bench_venue) if bench_venue else None,
                 "benchmark_symbol": benchmark, "open_positions_at_end": test.open_positions},
        "challenge": {
            "validation_cost_x2": _slim(val_cost2.metrics),
            "validation_delay_1bar": _slim(val_delay.metrics),
            "test_cost_x2": _slim(test_cost2.metrics),
            "test_delay_1bar": _slim(test_delay.metrics),
            "neighbors": neighbors,
            "neighbor_positive_frac": round(sum(1 for n in ok_n if n["sharpe"] > 0) / len(ok_n), 3) if ok_n else None,
            "neighbor_median_sharpe": float(np.median([n["sharpe"] for n in ok_n])) if ok_n else None,
            "pre_test_sharpe": round(pre_run.metrics["sharpe"], 4),
            "subperiods": subperiods,
            "subperiod_positive_frac": round(sum(1 for x in subperiods if x["total_return"] > 0) / 4, 3),
            "top5_concentration_pre_test": pre_run.metrics.get("top5_concentration"),
            "pre_test_trades": pre_run.metrics["n_trades"],
        },
        "leakage": leakage,
        "equity_curve": {"train": _curve(train), "validation": _curve(val), "test": _curve(test),
                         "test_benchmark": _bh_curve(panel, [benchmark], s, e),
                         "test_universe_bh": _bh_curve(panel, spec["universe"], s, e)},
        "test_trades": [{k: (round(x, 6) if isinstance(x, float) else x) for k, x in t.items()}
                        for t in test.trades[-50:]],
    }
    return out


def _leak_notes(venue, llm_authored, before_cutoff, reuse) -> list[str]:
    notes = []
    if llm_authored and before_cutoff > 0:
        notes.append(f"{before_cutoff:.0%} of the test window precedes the model's knowledge cutoff: the model may "
                     "already know how markets moved then, so the hypothesis can embed hindsight. Treat test "
                     "results as optimistic; live paper results are the clean evidence.")
    if reuse:
        notes.append(f"This test window overlaps {reuse} earlier experiment(s); significance is deflated for "
                     f"{reuse + 1} trials, and the agent saw earlier test outcomes when forming this idea.")
    if venue == "NASDAQ":
        notes.append("Universe is today's liquid US large caps and broad ETFs (survivorship/selection bias: backtests overstate). "
                     "Bars are split/dividend-adjusted; historical spreads are modelled, not observed.")
    else:
        notes.append("Coinbase-only volume is used for liquidity (conservative); historical spreads are modelled.")
    return notes
