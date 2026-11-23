"""Deterministic accounting.

Cash is the sum of an append-only ``cash_ledger``. Positions are kept per strategy sleeve
(strategy_id, symbol); the account position in a symbol is the sum across sleeves.
``rebuild_from_fills`` recomputes everything from fills to verify integrity.
"""
from __future__ import annotations

import math

from .. import clock, db
from ..config import load_config
from ..marketdata import store
from ..venues import venue_of

OPERATOR_SLEEVE = 0  # positions not owned by a strategy (none expected; reserved)
EPS = 1e-9


def init_account() -> None:
    if db.get_meta("account_started_at"):
        return
    cfg = load_config()
    with db.tx():
        if db.get_meta("account_started_at"):
            return
        ts = clock.now()
        db.ex("INSERT INTO cash_ledger(ts, delta, kind, note) VALUES(?,?,?,?)",
              (ts, cfg.initial_capital, "deposit", "initial virtual capital"))
        db.set_meta("account_started_at", str(ts))
        db.set_meta("initial_capital", str(cfg.initial_capital))


def cash() -> float:
    r = db.q1("SELECT COALESCE(SUM(delta), 0) AS c FROM cash_ledger")
    return float(r["c"])


def sleeve_positions(strategy_id: int | None = None) -> list[dict]:
    if strategy_id is None:
        rows = db.q("SELECT * FROM positions WHERE ABS(qty) > 1e-12 ORDER BY symbol")
    else:
        rows = db.q("SELECT * FROM positions WHERE strategy_id=? AND ABS(qty) > 1e-12", (strategy_id,))
    return [dict(r) for r in rows]


def account_positions() -> dict[str, float]:
    rows = db.q("SELECT symbol, SUM(qty) AS q FROM positions GROUP BY symbol HAVING ABS(SUM(qty)) > 1e-12")
    return {r["symbol"]: float(r["q"]) for r in rows}


def sleeve_qty(strategy_id: int, symbol: str) -> float:
    r = db.q1("SELECT qty FROM positions WHERE strategy_id=? AND symbol=?", (strategy_id, symbol))
    return float(r["qty"]) if r else 0.0


def marks(symbols) -> dict[str, tuple]:
    return {s: store.mark_price(s) for s in symbols}


def equity_breakdown() -> dict:
    pos = account_positions()
    c = cash()
    m = marks(pos.keys())
    values = {}
    stale = []
    for s, q in pos.items():
        px, as_of, basis = m[s]
        if px is None:
            stale.append(s)
            px = 0.0
        values[s] = {"qty": q, "mark": px, "value": q * px, "as_of": as_of, "basis": basis}
    gross = sum(v["value"] for v in values.values())
    crypto = sum(v["value"] for s, v in values.items() if venue_of(s) == "CRYPTO")
    return {"cash": c, "positions": values, "gross": gross, "crypto_gross": crypto,
            "equity": c + gross, "unpriced": stale}


def sleeve_equity(strategy_id: int, allocation: float) -> dict:
    """Strategy capital = allocation + realised P&L (net of fees) + unrealised P&L."""
    rows = db.q("SELECT * FROM positions WHERE strategy_id=?", (strategy_id,))
    realized = sum(r["realized_pnl"] for r in rows)
    unreal = 0.0
    value = 0.0
    cost = 0.0
    for r in rows:
        if abs(r["qty"]) < EPS:
            continue
        px, _, _ = store.mark_price(r["symbol"])
        px = px if px is not None else r["avg_cost"]
        value += r["qty"] * px
        cost += r["qty"] * r["avg_cost"]
        unreal += r["qty"] * (px - r["avg_cost"])
    fees = sum(r["fees_paid"] for r in rows)
    return {"allocation": allocation, "realized": realized, "unrealized": unreal,
            "fees": fees, "invested_value": value, "invested_cost": cost,
            "equity": allocation + realized + unreal, "free_cash": allocation + realized - cost}


def sleeve_peak(strategy_id: int, allocation: float, equity: float) -> float:
    stored = db.get_meta(f"sleeve_peak:{strategy_id}")
    return max(equity, float(stored) if stored is not None else allocation)


def mark_sleeve_peak(strategy_id: int, allocation: float, new_allocation: float | None = None) -> None:
    equity = sleeve_equity(strategy_id, allocation)["equity"]
    peak = sleeve_peak(strategy_id, allocation, equity)
    if new_allocation is not None:
        # Issue/redeem units at the pre-flow value: the flow alone cannot change drawdown.
        after = equity + (new_allocation - allocation)
        if equity > 0 and after > 0:
            peak *= after / equity
    db.set_meta(f"sleeve_peak:{strategy_id}", str(peak))


def apply_fill(order: dict, qty: float, price: float, fee: float, ref: dict) -> int:
    """Book a (partial) fill atomically: fill row, cash, sleeve position, order progress."""
    if not all(math.isfinite(v) for v in (qty, price, fee)) or qty <= 0 or price <= 0 or fee < 0:
        raise ValueError("fill requires positive finite quantity/price and a nonnegative finite fee")
    ts = clock.now()
    with db.tx():
        current = db.q1("SELECT * FROM orders WHERE id=?", (order["id"],))
        if current is None or current["status"] not in ("submitted", "partially_filled"):
            raise ValueError("cannot fill an order that is not open")
        order = dict(current)
        side = order["side"]
        if side not in ("buy", "sell") or qty > order["qty"] - order["filled_qty"] + EPS:
            raise ValueError("fill exceeds the remaining order quantity or has an invalid side")
        if side == "buy" and qty * price + fee > cash() + 1e-6:
            raise ValueError("fill exceeds available cash (no leverage)")
        fid = db.ex(
            "INSERT INTO fills(order_id, ts, symbol, side, qty, price, fee, ref_bid, ref_ask, ref_ts, price_basis, "
            "spread_bps, slippage_bps, notes) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (order["id"], ts, order["symbol"], side, qty, price, fee, ref.get("bid"), ref.get("ask"), ref.get("ts"),
             ref.get("basis"), ref.get("spread_bps"), ref.get("slippage_bps"), ref.get("notes")))
        sid = order["strategy_id"] if order["strategy_id"] is not None else OPERATOR_SLEEVE
        notional = qty * price
        db.ex("INSERT INTO cash_ledger(ts, delta, kind, fill_id, strategy_id, note) VALUES(?,?,?,?,?,?)",
              (ts, -notional if side == "buy" else notional, side, fid, sid, f"{side} {qty:g} {order['symbol']} @ {price:.6g}"))
        if fee:
            db.ex("INSERT INTO cash_ledger(ts, delta, kind, fill_id, strategy_id, note) VALUES(?,?,?,?,?,?)",
                  (ts, -fee, "fee", fid, sid, f"fees on fill {fid}"))
        pos = db.q1("SELECT * FROM positions WHERE strategy_id=? AND symbol=?", (sid, order["symbol"]))
        if pos is None:
            db.ex("INSERT INTO positions(strategy_id, symbol, venue, qty, avg_cost, realized_pnl, fees_paid, opened_at, updated_at) "
                  "VALUES(?,?,?,0,0,0,0,?,?)", (sid, order["symbol"], order["venue"], ts, ts))
            pos = db.q1("SELECT * FROM positions WHERE strategy_id=? AND symbol=?", (sid, order["symbol"]))
        q0, c0, rp, fp = pos["qty"], pos["avg_cost"], pos["realized_pnl"], pos["fees_paid"]
        if side == "buy":
            q1 = q0 + qty
            c1 = (q0 * c0 + notional) / q1
            rp1 = rp - fee
            opened = pos["opened_at"] if q0 > EPS else ts
        else:
            if qty > q0 + EPS:
                raise ValueError(f"sell {qty} exceeds sleeve position {q0} (no shorting)")
            q1 = q0 - qty
            c1 = c0 if q1 > EPS else 0.0
            rp1 = rp + qty * (price - c0) - fee
            opened = pos["opened_at"] if q1 > EPS else None
        if abs(q1) < 1e-10:
            q1 = 0.0
        db.ex("UPDATE positions SET qty=?, avg_cost=?, realized_pnl=?, fees_paid=?, opened_at=?, updated_at=? "
              "WHERE strategy_id=? AND symbol=?", (q1, c1, rp1, fp + fee, opened, ts, sid, order["symbol"]))
        filled = order["filled_qty"] + qty
        avg = ((order["avg_price"] or 0) * order["filled_qty"] + notional) / filled
        status = "filled" if filled >= order["qty"] - 1e-9 else "partially_filled"
        db.ex("UPDATE orders SET filled_qty=?, avg_price=?, fees=fees+?, status=?, closed_at=? WHERE id=?",
              (filled, avg, fee, status, ts if status == "filled" else None, order["id"]))
        if side == "buy" and order["strategy_id"] is not None:
            db.ex("UPDATE strategy_state SET entry_price=? WHERE strategy_id=? AND symbol=? AND in_position=1",
                  (avg, order["strategy_id"], order["symbol"]))
    return fid


def _replay(until: float | None = None) -> tuple[float, dict[tuple, float]]:
    """Cash and sleeve positions from deposits, fills, splits and dividend credits (before ``until``)."""
    args = () if until is None else (until,)

    def before(col: str) -> str:
        return "" if until is None else f" AND {col} < ?"
    c = float(db.q1("SELECT COALESCE(SUM(delta),0) d FROM cash_ledger WHERE kind='deposit'" + before("ts"), args)["d"])
    pos: dict[tuple, float] = {}
    # replay fills in id order; each split right after the last fill that existed when it was applied
    fills = [("fill", f["id"], 0, f) for f in
             db.q("SELECT f.*, o.strategy_id FROM fills f JOIN orders o ON o.id=f.order_id WHERE 1=1"
                  + before("f.ts") + " ORDER BY f.id", args)]
    splits = [("split", (db.loads(a["detail"], {}) or {}).get("after_fill_id", 0), 1, a)
              for a in db.q("SELECT * FROM corporate_actions WHERE kind='split'" + before("ts") + " ORDER BY id", args)]
    for kind, _, _, f in sorted(fills + splits, key=lambda e: (e[1], e[2])):
        if kind == "split":
            for k in list(pos):
                if k[1] == f["symbol"]:
                    pos[k] *= f["ratio"]
            continue
        sid = f["strategy_id"] if f["strategy_id"] is not None else OPERATOR_SLEEVE
        k = (sid, f["symbol"])
        if f["side"] == "buy":
            c -= f["qty"] * f["price"] + f["fee"]
            pos[k] = pos.get(k, 0.0) + f["qty"]
        else:
            c += f["qty"] * f["price"] - f["fee"]
            pos[k] = pos.get(k, 0.0) - f["qty"]
    for a in db.q("SELECT detail FROM corporate_actions WHERE kind='dividend'" + before("ts"), args):
        c += sum(float(v) for v in ((db.loads(a["detail"], {}) or {}).get("credits") or {}).values())
    return c, pos


def rebuild_from_fills() -> dict:
    """Recompute cash and positions from fills + deposits (integrity check)."""
    c, pos = _replay()
    return {"cash": c, "positions": {f"{k[0]}:{k[1]}": v for k, v in pos.items() if abs(v) > 1e-10}}


def holdings_at(ts: float, symbol: str) -> dict[int, float]:
    """Shares of ``symbol`` each sleeve held just before ``ts`` (replayed from fills and splits)."""
    _, pos = _replay(until=ts)
    return {sid: q for (sid, sym), q in pos.items() if sym == symbol and q > 1e-10}


def credit_dividend(strategy_id: int, symbol: str, amount: float, note: str) -> None:
    """Book a cash dividend to a sleeve: cash in, and counted as that sleeve's realised P&L.
    Callers hold a transaction and record the matching corporate_actions row in it."""
    if not math.isfinite(amount) or amount <= 0:
        raise ValueError("dividend credit must be positive and finite")
    if db.q1("SELECT 1 FROM positions WHERE strategy_id=? AND symbol=?", (strategy_id, symbol)) is None:
        raise ValueError(f"no sleeve position row for {strategy_id}:{symbol}")
    ts = clock.now()
    db.ex("INSERT INTO cash_ledger(ts, delta, kind, strategy_id, note) VALUES(?,?,?,?,?)",
          (ts, amount, "dividend", strategy_id, note))
    db.ex("UPDATE positions SET realized_pnl=realized_pnl+?, updated_at=? WHERE strategy_id=? AND symbol=?",
          (amount, ts, strategy_id, symbol))


def integrity_check() -> dict:
    rebuilt = rebuild_from_fills()
    live_pos = {f"{r['strategy_id']}:{r['symbol']}": r["qty"] for r in sleeve_positions()}
    cash_ok = abs(rebuilt["cash"] - cash()) < 1e-6
    pos_ok = set(rebuilt["positions"]) == set(live_pos) and all(
        abs(rebuilt["positions"][k] - live_pos[k]) < 1e-8 for k in live_pos)
    neg = [k for k, v in live_pos.items() if v < -1e-10]
    return {"ok": cash_ok and pos_ok and not neg and cash() > -1e-6, "cash_ok": cash_ok,
            "positions_ok": pos_ok, "negative_positions": neg, "cash": cash(), "rebuilt_cash": rebuilt["cash"]}
