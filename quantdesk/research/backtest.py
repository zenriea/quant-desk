"""Event-driven backtester (long-only, cash account, next-bar execution).

Timing: signals use data up to the CLOSE of bar t; orders execute at the OPEN of bar
t+1+delay. Fills pay half-spread + square-root impact + venue fees. No position is ever
opened with information from the bar it trades on.
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field

import numpy as np

from ..trading import costs as C
from ..trading.liquidity import estimates
from ..venues import VENUES
from . import metrics
from .strategy import PosState, Signals, entry_weight, step


@dataclass
class BTResult:
    equity: np.ndarray
    ts: np.ndarray
    trades: list[dict]
    metrics: dict
    exposure: float
    fees_paid: float
    slippage_paid: float
    open_positions: int
    benchmark: dict = field(default_factory=dict)


def liquidity_arrays(panel: dict, venue: str, lookback: int | None = None):
    """Same 20-day estimator as paper execution, using bars strictly BEFORE t."""
    adv, sig = estimates(panel["close"], panel["volume"], VENUES[venue].bars_per_day, lookback)
    return adv[:-1], sig[:-1]


def run(spec: dict, panel: dict, sig: Signals, costs_cfg: dict, start: int, end: int,
        capital: float = 100_000.0, delay: int = 0, liquidity=None,
        max_participation: float = 0.01, events: list | None = None, usable_after: int = 0) -> BTResult:
    """A signal on bar t is usable ``usable_after`` seconds after the bar closes (the venue's data delay;
    see ``freshness.signal_usable_after``) and fills at the first bar OPEN at or after that moment, which
    crosses session and weekend gaps. ``delay`` adds whole bars on top (the +1-bar stress test).
    ``events``, when given, receives one dict per signal, fill and skipped order (for the live replay)."""
    venue = spec["venue"]
    v = VENUES[venue]
    syms = panel["symbols"]
    N = len(syms)
    opn, cls = panel["open"], panel["close"]
    adv, sigma = liquidity if liquidity is not None else liquidity_arrays(panel, venue)
    half_spread = costs_cfg["backtest_half_spread_bps"]
    whole = v.qty_decimals == 0
    cols = [syms.index(s) for s in spec["universe"] if s in syms]

    states = [PosState() for _ in range(N)]
    cash = capital
    qty = np.zeros(N)
    cost_basis = np.zeros(N)       # total cash paid incl. fees, for trade PnL
    entry_info: dict[int, dict] = {}
    pending: dict[int, list] = {}  # bar index -> [(col, action, reason, weight)]
    trades: list[dict] = []
    equity = []
    exposure = []
    fees_paid = 0.0
    slip_paid = 0.0
    last_mark = np.zeros(N)
    sub = StepView(sig, cols)  # built once: slicing per bar made long histories quadratic
    ts_all = np.asarray(panel["ts"], dtype=np.int64)
    # First bar whose open is at or after bar t's close + data delay: the earliest open a signal can reach.
    eligible = np.searchsorted(ts_all, ts_all + v.bar_seconds + int(usable_after), side="left")

    for t in range(start, end):
        # 1) execute orders due at this bar's open
        for col, action, reason, w in pending.pop(t, []):
            px_ref = opn[t, col]
            if not np.isfinite(px_ref):
                pending.setdefault(t + 1, []).append((col, action, reason, w))
                continue
            if action == "buy" and qty[col] == 0:
                eq_prev = equity[-1] if equity else capital
                notional = min(w * eq_prev, cash * 0.995)
                if np.isfinite(adv[t, col]):
                    notional = min(notional, max_participation * adv[t, col])  # same cap as the live simulator
                if notional <= 1.0:
                    states[col].in_position = False
                    if events is not None:
                        events.append({"t": t, "kind": "skip", "side": "buy", "col": col, "reason": "order too small"})
                    continue
                bps = half_spread + C.impact_bps(notional, adv[t, col], sigma[t, col], costs_cfg)
                px = C.apply_slippage("buy", px_ref, bps)
                q = notional / px
                if whole:
                    q = math.floor(q)
                if q <= 0:
                    states[col].in_position = False
                    continue
                fee = C.fees(venue, "buy", q, px, costs_cfg)
                if q * px + fee > cash:
                    q = math.floor((cash - fee) / px) if whole else (cash - fee) / px
                    if q <= 0:
                        states[col].in_position = False
                        continue
                cash -= q * px + fee
                qty[col] = q
                cost_basis[col] = q * px + fee
                fees_paid += fee
                slip_paid += q * (px - px_ref)
                states[col].entry_price = px
                entry_info[col] = {"symbol": syms[col], "entry_ts": int(panel["ts"][t]), "entry_px": px,
                                   "qty": q, "entry_bar": t}
                if events is not None:
                    events.append({"t": t, "kind": "fill", "side": "buy", "col": col, "px": float(px), "qty": float(q)})
            elif action == "sell" and qty[col] > 0:
                q = qty[col]
                if np.isfinite(adv[t, col]) and q * px_ref > max_participation * adv[t, col]:
                    # Same cap as the live simulator; live reconciles the remainder on the next bar.
                    pending.setdefault(t + 1, []).append((col, action, reason, w))
                    q = max_participation * adv[t, col] / px_ref
                    q = math.floor(q) if whole else q
                    if q <= 0:
                        continue
                notional = q * px_ref
                bps = half_spread + C.impact_bps(notional, adv[t, col], sigma[t, col], costs_cfg)
                px = C.apply_slippage("sell", px_ref, bps)
                fee = C.fees(venue, "sell", q, px, costs_cfg)
                proceeds = q * px - fee
                cash += proceeds
                fees_paid += fee
                slip_paid += q * (px_ref - px)
                if events is not None:
                    events.append({"t": t, "kind": "fill", "side": "sell", "col": col, "px": float(px), "qty": float(q),
                                   "reason": reason})
                info = entry_info.setdefault(col, {})
                info["_proceeds"] = info.get("_proceeds", 0.0) + proceeds
                info.setdefault("_basis", cost_basis[col])
                cost_basis[col] *= 1 - q / qty[col]
                qty[col] -= q
                if qty[col] > 1e-12:
                    continue
                info = entry_info.pop(col)
                basis = info.pop("_basis")
                pnl = info.pop("_proceeds") - basis
                trades.append({**info, "exit_ts": int(panel["ts"][t]), "exit_px": px, "reason": reason,
                               "pnl": float(pnl), "return": float(pnl / basis) if basis else 0.0,
                               "bars": t - info.get("entry_bar", t)})
                qty[col] = 0
                cost_basis[col] = 0
        # 2) mark to market at the close
        # carry the last finite close for missing bars (past data only)
        mark = np.where(np.isfinite(cls[t]), cls[t], last_mark)  # always finite (last_mark starts at 0)
        last_mark = mark
        pos_val = float(np.sum(qty * mark))
        eq = cash + pos_val
        equity.append(eq)
        exposure.append(pos_val / eq if eq > 0 else 0.0)
        # 3) decide at the close; act at t+1+delay
        if t >= end - 1:
            break
        sub_states = [states[c] for c in cols]
        res = step(spec, sub_states, t, sub, cls[t, cols])
        due = int(eligible[t]) + delay
        if events is not None:
            for k, reason in res.exit:
                events.append({"t": t, "kind": "signal", "side": "sell", "col": cols[k], "reason": reason,
                               "executes": due < end})
            for k in res.enter:
                events.append({"t": t, "kind": "signal", "side": "buy", "col": cols[k], "reason": "entry",
                               "executes": due < end})
        if due >= end:
            continue
        for k, reason in res.exit:
            col = cols[k]
            pending.setdefault(due, []).append((col, "sell", reason, 0.0))
        for k in res.enter:
            col = cols[k]
            w = entry_weight(spec, sig.vol[t, col])
            if w <= 0:
                states[col].in_position = False
                if events is not None:
                    events.append({"t": t, "kind": "skip", "side": "buy", "col": col, "reason": "zero target weight"})
                continue
            pending.setdefault(due, []).append((col, "buy", "entry", w))

    eq_arr = np.array(equity)
    m = metrics.perf(eq_arr, v.periods_per_year)
    m.update(metrics.trade_stats(trades))
    m["exposure"] = float(np.mean(exposure)) if exposure else 0.0
    m["fees_paid"] = float(fees_paid)
    m["slippage_paid"] = float(slip_paid)
    m["cost_drag"] = float((fees_paid + slip_paid) / capital)
    return BTResult(eq_arr, panel["ts"][start:start + len(equity)], trades, m, m["exposure"],
                    fees_paid, slip_paid, int((qty > 0).sum()))


class StepView:
    """Column-subset view of Signals so the state machine sees only the strategy universe."""

    def __init__(self, sig: Signals, cols: list[int]):
        self.entry = sig.entry[:, cols]
        self.exit = sig.exit[:, cols]
        self.score = sig.score[:, cols] if sig.score is not None else None
        self.vol = sig.vol[:, cols]


def buy_and_hold(panel: dict, symbols: list[str], start: int, end: int, costs_cfg: dict,
                 venue: str, capital: float = 100_000.0) -> dict:
    """Equal-weight buy-and-hold benchmark over the same window (pays entry costs)."""
    syms = panel["symbols"]
    cols = [syms.index(s) for s in symbols if s in syms]
    opn, cls = panel["open"], panel["close"]
    if not cols or end - start < 2:
        return {}
    alloc = capital / len(cols)
    qty = {}
    for c in cols:
        px = opn[start, c] if np.isfinite(opn[start, c]) else cls[start, c]
        if not np.isfinite(px):
            continue
        px = px * (1 + (costs_cfg["backtest_half_spread_bps"] + costs_cfg["base_slippage_bps"]) / 1e4)
        fee = C.fees(venue, "buy", alloc / px, px, costs_cfg)
        qty[c] = (alloc - fee) / px
    eq = [capital]  # start before entry so the benchmark pays its entry costs and first open-to-close move
    last = {c: np.nan for c in cols}
    for t in range(start, end):
        val = capital - alloc * len(qty)
        for c, q in qty.items():
            if np.isfinite(cls[t, c]):
                last[c] = cls[t, c]
            val += q * (last[c] if np.isfinite(last[c]) else 0)
        eq.append(val)
    m = metrics.perf(np.array(eq), VENUES[venue].periods_per_year)
    return m
