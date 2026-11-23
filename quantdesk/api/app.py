"""HTTP API + static UI. Run with python -m quantdesk.api.app.

Security
  * Password login -> opaque, revocable session cookie (HttpOnly, SameSite=Strict,
    Secure when QD_COOKIE_SECURE=1). Without QD_ADMIN_PASSWORD only loopback clients are
    served.
  * State-changing requests must carry header ``X-QD: 1`` (blocks cross-site forms).
  * Optional QD_VIEWER_PASSWORD signs in a read-only viewer: results only, no controls,
    limits, audit log or model prompts.
  * QD_PUBLIC_VIEW=1 gives that same read-only view to anyone without a session.
  * Login attempts are rate limited. Secrets never leave the server.
"""
from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import logging
import secrets
import time
from collections import defaultdict
from pathlib import Path

from fastapi import Depends, FastAPI, HTTPException, Query, Request, Response
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from starlette.concurrency import run_in_threadpool
from pydantic import BaseModel, Field

from .. import clock, db, settings, venues
from ..agent import journal
from ..agent.llm import budget_status, llm_status
from ..config import DEFAULT_SETTINGS, GATE_PRESETS, SETTING_BOUNDS, load_config
from ..marketdata import store
from ..research.sandbox import isolation_report
from ..trading import ledger, live, oms

log = logging.getLogger(__name__)

WEB = Path(__file__).resolve().parents[2] / "web"
SESSION_TTL = 12 * 3600
cfg = load_config()
cfg.check_api_bind()
db.init(cfg.db_path)

app = FastAPI(title="QuantDesk", docs_url=None, redoc_url=None, openapi_url=None)
_attempts: dict[str, list[float]] = defaultdict(list)


# ------------------------------------------------------------------ auth
OPERATOR, VIEWER = "operator", "viewer"


def _session_digest(token: str | None) -> str | None:
    if not token or len(token) != 64 or any(c not in "0123456789abcdef" for c in token):
        return None
    return hmac.new(cfg.session_secret.encode(), token.encode("ascii"), hashlib.sha256).hexdigest()


def _role(token: str | None) -> str | None:
    digest = _session_digest(token)
    if digest is None:
        return None
    row = db.q1("SELECT role FROM web_sessions WHERE token_digest=? AND expires_at>?", (digest, time.time()))
    return row["role"] if row else None


def _start_session(role: str, ip: str, previous: str | None) -> str:
    token = secrets.token_hex(32)
    now = time.time()
    with db.tx() as conn:
        conn.execute("DELETE FROM web_sessions WHERE expires_at<=?", (now,))
        conn.execute("DELETE FROM web_sessions WHERE token_digest=?", (_session_digest(previous),))
        conn.execute("INSERT INTO web_sessions(token_digest, role, expires_at) VALUES(?,?,?)",
                     (_session_digest(token), role, now + SESSION_TTL))
        conn.execute("INSERT INTO audit_log(ts, actor, action, detail) VALUES(?,?,?,?)",
                     (clock.now(), role, "login", db.dumps({"ip": ip})))
    return token


def _client_ip(request: Request) -> str:
    return request.client.host if request.client else "?"


def require_auth(request: Request) -> str:
    if not cfg.admin_password:
        if (not cfg.loopback_bind or _client_ip(request) not in ("127.0.0.1", "::1", "testclient")
                or "x-forwarded-for" in request.headers or "forwarded" in request.headers
                or request.url.hostname not in ("127.0.0.1", "::1", "localhost", "testserver")):
            raise HTTPException(403, "QD_ADMIN_PASSWORD is not set: remote access disabled")
        return OPERATOR
    role = _role(request.cookies.get("qd_session"))
    if role is None:
        if cfg.public_view:
            return VIEWER
        raise HTTPException(401, "login required")
    return role


def require_operator(request: Request):
    """Reads that only the operator sees: limits, audit log, jobs and model prompts."""
    if require_auth(request) != OPERATOR:
        raise HTTPException(403, "read-only view")


def _require_csrf(request: Request):
    if request.headers.get("x-qd") != "1":
        raise HTTPException(403, "missing X-QD header")
    _require_same_origin(request)


def require_write(request: Request):
    require_operator(request)
    _require_csrf(request)


def _require_same_origin(request: Request):
    origin = request.headers.get("origin")
    expected = cfg.public_origin or str(request.base_url).rstrip("/")
    if origin and origin != expected:
        raise HTTPException(403, "cross-origin writes are not allowed")


@app.middleware("http")
async def security_headers(request: Request, call_next):
    resp = await call_next(request)
    resp.headers["X-Content-Type-Options"] = "nosniff"
    resp.headers["X-Frame-Options"] = "DENY"
    resp.headers["Referrer-Policy"] = "no-referrer"
    if request.url.path.startswith("/api/"):
        resp.headers["Cache-Control"] = "no-store"
    elif request.url.path in ("/", "/static/app.js", "/static/app.css", "/static/ui-model.mjs"):
        resp.headers["Cache-Control"] = "no-cache"
    resp.headers["Content-Security-Policy"] = (
        "default-src 'self'; img-src 'self' data:; style-src 'self' 'unsafe-inline' https://fonts.googleapis.com; "
        "font-src https://fonts.gstatic.com; script-src 'self'; connect-src 'self'")
    return resp


class LoginBody(BaseModel):
    password: str = Field(max_length=1024, strict=True)


class AskBody(BaseModel):
    question: str = Field(min_length=1, max_length=1000, strict=True)


@app.post("/api/login")
async def login(body: LoginBody, request: Request, response: Response):
    _require_same_origin(request)
    ip = _client_ip(request)
    now = time.time()
    _attempts[ip] = [t for t in _attempts[ip] if now - t < 60]
    if len(_attempts[ip]) >= 5:
        raise HTTPException(429, "too many attempts; wait a minute")
    _attempts[ip].append(now)
    pw = body.password
    if not cfg.admin_password:
        raise HTTPException(400, "no password configured; local access only")
    digest = hashlib.sha256(pw.encode()).digest()
    is_operator = hmac.compare_digest(digest, hashlib.sha256(cfg.admin_password.encode()).digest())
    # both comparisons always run, so timing does not reveal which password matched
    is_viewer = bool(cfg.viewer_password) & hmac.compare_digest(digest, hashlib.sha256(cfg.viewer_password.encode()).digest())
    if not (is_operator or is_viewer):
        await asyncio.sleep(1.0)
        raise HTTPException(401, "wrong password")
    role = OPERATOR if is_operator else VIEWER
    token = await run_in_threadpool(_start_session, role, ip, request.cookies.get("qd_session"))
    response.set_cookie("qd_session", token, max_age=SESSION_TTL, httponly=True,
                        samesite="strict", secure=cfg.cookie_secure, path="/")
    return {"ok": True, "role": role}


@app.post("/api/logout", dependencies=[Depends(require_auth), Depends(_require_csrf)])
def logout(request: Request, response: Response):
    if cfg.admin_password:
        db.ex("DELETE FROM web_sessions WHERE token_digest=?", (_session_digest(request.cookies.get("qd_session")),))
    response.delete_cookie("qd_session", path="/", httponly=True, samesite="strict", secure=cfg.cookie_secure)
    return {"ok": True}


@app.get("/api/session")
def session(request: Request):
    try:
        role = require_auth(request)
    except HTTPException:
        return {"authenticated": False, "password_required": bool(cfg.admin_password)}
    anonymous = bool(cfg.admin_password) and _role(request.cookies.get("qd_session")) is None
    return {"authenticated": True, "password_required": bool(cfg.admin_password), "role": role,
            "anonymous": anonymous, "public_view": cfg.public_view}


@app.get("/api/health")
async def health():
    r = db.q1("SELECT heartbeat_at FROM worker_lock WHERE id=1")
    age = clock.now() - r["heartbeat_at"] if r else None
    ok = age is not None and age < 60
    return JSONResponse({"ok": ok, "worker_heartbeat_age_sec": age}, status_code=200 if ok else 503)


# ------------------------------------------------------------------ read models
def _state() -> dict:
    now = clock.now()
    eb = ledger.equity_breakdown()
    init = float(db.get_meta("initial_capital") or cfg.initial_capital)
    snap = db.q1("SELECT * FROM equity_snapshots ORDER BY ts DESC LIMIT 1")
    first = db.q1("SELECT * FROM equity_snapshots ORDER BY ts ASC LIMIT 1")
    peak = max(eb["equity"], (db.q1("SELECT MAX(peak_equity) p FROM equity_snapshots")["p"] or 0))
    fees = db.q1("SELECT COALESCE(SUM(fee),0) f FROM fills")["f"]
    pos_rows = []
    for p in ledger.sleeve_positions():
        px, as_of, basis = store.mark_price(p["symbol"])
        st = db.q1("SELECT * FROM strategies WHERE id=?", (p["strategy_id"],))
        entry_order = db.q1("SELECT id, rationale, created_at FROM orders WHERE strategy_id=? AND symbol=? AND side='buy' "
                            "AND filled_qty>0 ORDER BY id DESC LIMIT 1", (p["strategy_id"], p["symbol"]))
        spec = db.loads(st["spec"], {}) if st else {}
        exits = []
        if spec.get("exit"):
            exits.append(f"Rule: {spec['exit']} {spec.get('fixed_params') or ''}".strip())
        if spec.get("stop_loss_pct"):
            exits.append(f"Stop-loss -{spec['stop_loss_pct']:.0%} on a bar close (from ${p['avg_cost']:,.2f}: "
                         f"${p['avg_cost'] * (1 - spec['stop_loss_pct']):,.2f})")
        if spec.get("take_profit_pct"):
            exits.append(f"Take-profit +{spec['take_profit_pct']:.0%}")
        if spec.get("max_hold_bars"):
            exits.append(f"Time stop after {spec['max_hold_bars']} bars")
        if spec.get("rank_by"):
            exits.append(f"Rotated out if it drops out of the top {spec.get('top_k')} by {spec['rank_by']}")
        val = p["qty"] * (px or 0)
        pos_rows.append({
            "strategy_id": p["strategy_id"], "strategy": f"{st['name']} v{st['version']}" if st else "-",
            "probe": bool(st["probe"]) if st else False,
            "symbol": p["symbol"], "venue": p["venue"], "qty": p["qty"], "avg_cost": p["avg_cost"], "mark": px,
            "mark_as_of": as_of, "mark_basis": basis, "value": val,
            "unrealized": p["qty"] * ((px or p["avg_cost"]) - p["avg_cost"]),
            "unrealized_pct": ((px or p["avg_cost"]) / p["avg_cost"] - 1) if p["avg_cost"] else 0,
            "weight": val / eb["equity"] if eb["equity"] else 0, "opened_at": p["opened_at"],
            "entry_rationale": entry_order["rationale"] if entry_order else None,
            "entry_order_id": entry_order["id"] if entry_order else None,
            "thesis": st["thesis"] if st else None, "exit_conditions": exits,
            "exit_plan": st["exit_plan"] if st else None,
        })
    agent_status = db.row_dict(db.q1("SELECT * FROM agent_status WHERE id=1"), ["next_actions"])
    hb = db.q1("SELECT * FROM worker_lock WHERE id=1")
    bench = snap["benchmark_equity"] if snap else None
    data_status = store.status_all()
    uni = settings.get("universe")
    from ..worker import market_symbols
    tracked = {s for syms in market_symbols().values() for s in syms}
    bad = [d for d in data_status if not d["ok"] and d["key"].split(":", 1)[-1] in tracked]
    if cfg.market_data_mode != "synthetic" and not cfg.alpaca_configured:
        nasdaq = set(uni["NASDAQ"])   # already covered by the "not configured" alert
        bad = [d for d in bad if d["key"].split(":", 1)[-1] not in nasdaq]
    llm_info = llm_status(cfg)
    return {
        "now": now,
        "mode": {"paper": True, "llm": llm_info["label"],
                 "market_data": cfg.market_data_mode, "alpaca_configured": cfg.alpaca_configured,
                 "alpaca_feed": cfg.alpaca_feed},
        "worker": {"alive": bool(hb and now - hb["heartbeat_at"] < 60),
                   "heartbeat_age": now - hb["heartbeat_at"] if hb else None,
                   "started_at": hb["started_at"] if hb else None},
        "controls": settings.get("agent") | {"drawdown_halt": db.get_meta("drawdown_halt") == "1",
                                             "system_halt": db.get_meta("system_halt") == "1",
                                             "emergency_stop_at": _float_meta("emergency_stop_at"),
                                             "emergency_cancelled": _float_meta("emergency_cancelled")},
        "account": {"equity": eb["equity"], "cash": eb["cash"], "gross": eb["gross"], "crypto_gross": eb["crypto_gross"],
                    "initial": init, "pnl": eb["equity"] - init, "return": eb["equity"] / init - 1,
                    "fees_paid": fees, "peak": peak, "drawdown": (eb["equity"] - peak) / peak if peak else 0,
                    "exposure": eb["gross"] / eb["equity"] if eb["equity"] else 0,
                    "benchmark_symbol": settings.get("benchmarks")["account"],
                    "benchmark_equity": bench,
                    "benchmark_return": (bench / init - 1) if bench else None,
                    "tracking_since": first["ts"] if first else None, "unpriced": eb["unpriced"]},
        "positions": sorted(pos_rows, key=lambda r: -r["value"]),
        "agent": agent_status,
        "sessions": venues.describe_session(now),
        "data": {"problems": bad, "count": len(data_status)},
        "budget": budget_status(),
        "limits": settings.get("risk"),
        "pipeline": _pipeline(now),
        "strategies": [dict(r) | {"spec": None} for r in db.q("SELECT id, name, version, venue, status, allocation, "
                                                                 "approved_allocation, created_at, probe FROM strategies "
                                                                 "WHERE status IN ('active','incubating') ORDER BY id")],
        "beliefs": _distinct_beliefs(),
        "last_fill_ts": db.q1("SELECT MAX(ts) t FROM fills")["t"],
        "counts": {k: db.q1(f"SELECT COUNT(*) n FROM {t}")["n"] for k, t in
                   (("experiments", "experiments"), ("hypotheses", "hypotheses"), ("orders", "orders"),
                    ("fills", "fills"), ("strategies", "strategies"))},
        "open_orders": [dict(o) for o in db.q("SELECT id, symbol, side, qty, filled_qty, status, eligible_at, expires_at, "
                                              "reason FROM orders WHERE status IN ('intent','submitted','partially_filled') ORDER BY id")],
    }


@app.get("/api/state", dependencies=[Depends(require_auth)])
async def state():
    return await run_in_threadpool(_state)


@app.get("/api/equity", dependencies=[Depends(require_auth)])
async def equity(points: int = Query(600, ge=2, le=3000)):
    def f():
        rows = db.q("SELECT ts, equity, benchmark_equity, crypto_benchmark_equity, gross, cash FROM equity_snapshots ORDER BY ts")
        if len(rows) > points:
            step = (len(rows) - 1) / (points - 1)
            idx = sorted({int(round(i * step)) for i in range(points)})
            rows = [rows[i] for i in idx]
        return [dict(r) for r in rows]
    return await run_in_threadpool(f)


@app.get("/api/journal", dependencies=[Depends(require_auth)])
async def get_journal(after_id: int = Query(0, ge=0), limit: int = Query(100, ge=1, le=500),
                      kind: str | None = None):
    return await run_in_threadpool(journal.recent, min(limit, 500), after_id, kind.split(",") if kind else None)


@app.get("/api/stream", dependencies=[Depends(require_auth)])
async def stream(request: Request, after_id: int = Query(0, ge=0)):
    resume = request.headers.get("last-event-id", "")
    if resume.isascii() and resume.isdigit() and len(resume) <= 19:
        after_id = max(after_id, int(resume))

    async def gen():
        last = after_id
        tick = 0
        while True:
            if await request.is_disconnected():
                break
            if cfg.admin_password and not cfg.public_view and not await run_in_threadpool(_role, request.cookies.get("qd_session")):
                break
            rows = await run_in_threadpool(journal.since, last, 50)
            for r in rows:
                last = max(last, r["id"])
                yield f"id: {r['id']}\nevent: journal\ndata: {json.dumps(r, default=str)}\n\n"
            if len(rows) == 50:
                await asyncio.sleep(0)
                continue
            tick += 1
            if tick % 5 == 0:
                yield f"event: ping\ndata: {int(time.time())}\n\n"
            await asyncio.sleep(2)
    return StreamingResponse(gen(), media_type="text/event-stream",
                             headers={"Cache-Control": "no-store", "X-Accel-Buffering": "no"})


def _rows(sql, params=(), json_fields=()):
    return [db.row_dict(r, json_fields) for r in db.q(sql, params)]


@app.get("/api/experiments", dependencies=[Depends(require_auth)])
async def experiments(limit: int = Query(100, ge=1, le=500)):
    return await run_in_threadpool(_rows, "SELECT e.id, e.hypothesis_id, e.created_at, e.completed_at, e.venue, e.kind, e.status, "
                                   "e.chosen_params, e.test, e.gates, e.leakage, e.error, h.title, h.author, "
                                   "s.id AS strategy_id, s.probe AS strategy_probe, s.status AS strategy_status FROM experiments e "
                                   "JOIN hypotheses h ON h.id=e.hypothesis_id "
                                   "LEFT JOIN strategies s ON s.id = (SELECT id FROM strategies WHERE experiment_id=e.id ORDER BY id DESC LIMIT 1) "
                                   "ORDER BY e.id DESC LIMIT ?", (limit,),
                                   ("chosen_params", "test", "gates", "leakage"))


@app.get("/api/experiments/{eid}", dependencies=[Depends(require_auth)])
async def experiment(eid: int):
    def f():
        from ..agent.research_loop import probe_blocker
        raw = db.q1("SELECT * FROM experiments WHERE id=?", (eid,))
        e = db.row_dict(raw,
                        ["spec", "windows", "chosen_params", "grid_results", "train", "validation", "test", "challenge",
                         "gates", "leakage", "equity_curve"])
        if not e:
            raise HTTPException(404)
        e["hypothesis"] = db.row_dict(db.q1("SELECT * FROM hypotheses WHERE id=?", (e["hypothesis_id"],)))
        e["decisions"] = _rows("SELECT * FROM decisions WHERE subject_type='experiment' AND subject_id=? ORDER BY id",
                               (eid,), ("details",))
        e["strategy"] = db.row_dict(db.q1("SELECT id, name, version, status, probe FROM strategies WHERE experiment_id=? ORDER BY id DESC", (eid,)))
        e["operator_probe_blocker"] = probe_blocker(dict(raw), operator=True)
        return e
    return await run_in_threadpool(f)


def _distinct_beliefs(limit: int = 10) -> list[dict]:
    """Newest active beliefs, one per claim per venue (older rows may hold several phrasings of one fact)."""
    from ..agent.research_loop import same_claim
    out: list[dict] = []
    for b in db.q("SELECT * FROM beliefs WHERE status='active' ORDER BY id DESC"):
        if not any(o["venue"] == b["venue"] and same_claim(o["statement"], b["statement"]) for o in out):
            out.append(db.row_dict(b, ["evidence"]))
    return out[:limit]


def _pipeline(now: float) -> dict:
    """Why the desk is or is not trading: recent research outcomes and the closest candidates."""
    from ..agent.research_loop import ranked_candidates
    since = now - 7 * 86400
    rows = [dict(r) for r in db.q("SELECT status, gates FROM experiments WHERE created_at>=?", (since,))]
    fails: dict[str, int] = {}
    for r in rows:
        for f in (db.loads(r["gates"], {}) or {}).get("failed") or []:
            fails[f] = fails.get(f, 0) + 1
    cands = ranked_candidates(since)
    counts = {k: db.q1(f"SELECT COUNT(*) n FROM strategies WHERE status IN ('active','incubating') AND probe={v}")["n"]
              for k, v in (("strategies", 0), ("probes", 1))}
    finished = [r for r in rows if r["status"] in ("passed", "failed")]
    return {"window_days": 7, "experiments": len(rows), "finished": len(finished),
            "passed": sum(r["status"] == "passed" for r in rows),
            "passed_unallocated": sum(1 for c in cands if c["passed"] and not c["allocated"] and not c["bar_size_mismatch"]),
            "profitable": sum(1 for c in cands if c["test_return"] > 0),
            "profitable_current_bar": sum(1 for c in cands if c["test_return"] > 0 and not c["bar_size_mismatch"]),
            "gate_failures": sorted(({"gate": k, "n": v} for k, v in fails.items()), key=lambda x: -x["n"]),
            "candidates": cands[:3],  # includes blocked records for the desk verdict
            "review_candidates": [c for c in cands if c["operator_blocker"] is None][:3],
            "live": counts, "probe": settings.get("probe")}


@app.get("/api/strategies", dependencies=[Depends(require_auth)])
async def strategies(limit: int = Query(100, ge=1, le=500)):
    def f():
        return [_strategy_model(s) for s in db.q("SELECT * FROM strategies ORDER BY id DESC LIMIT ?", (limit,))]
    return await run_in_threadpool(f)


def _strategy_model(s):
    from ..agent.review import live_stats
    d = db.row_dict(s, ["spec", "expectations", "invalidation"])
    d["live"] = live_stats(dict(s))
    d["symbols"] = _strategy_symbols(s["id"], (d["spec"] or {}).get("universe") or [])
    d["reviews"] = _rows("SELECT * FROM decisions WHERE subject_type='strategy' AND subject_id=? ORDER BY id DESC LIMIT 5",
                         (s["id"],), ("details",))
    return d


def _strategy_symbols(sid: int, universe: list[str]) -> list[dict]:
    """Every symbol a strategy may trade, with what it holds and has pending in each."""
    held = {p["symbol"]: p for p in ledger.sleeve_positions(sid)}
    pending: dict[str, str] = {}
    for o in db.q("SELECT symbol, side FROM orders WHERE strategy_id=? "
                  "AND status IN ('intent','submitted','partially_filled')", (sid,)):
        pending[o["symbol"]] = o["side"]
    out = []
    for sym in list(universe) + [x for x in held if x not in universe]:
        p = held.get(sym)
        row = {"symbol": sym, "venue": venues.venue_of(sym), "qty": p["qty"] if p else 0.0,
               "avg_cost": p["avg_cost"] if p else None, "mark": None, "value": 0.0, "unrealized": None,
               "pending": pending.get(sym)}
        if p:
            px = store.mark_price(sym)[0] or p["avg_cost"]
            row.update(mark=px, value=p["qty"] * px, unrealized=p["qty"] * (px - p["avg_cost"]))
        out.append(row)
    return out


@app.get("/api/strategies/{sid}", dependencies=[Depends(require_auth)])
async def strategy(sid: int):
    def f():
        s = db.q1("SELECT * FROM strategies WHERE id=?", (sid,))
        if not s:
            raise HTTPException(404, "strategy not found")
        return _strategy_model(s)
    return await run_in_threadpool(f)


@app.get("/api/orders", dependencies=[Depends(require_auth)])
async def orders(limit: int = Query(200, ge=1, le=500)):
    return await run_in_threadpool(_rows, "SELECT * FROM orders ORDER BY id DESC LIMIT ?", (limit,), ("signal_snapshot", "risk_check"))


@app.get("/api/orders/{oid}", dependencies=[Depends(require_auth)])
async def order(oid: int):
    def f():
        o = db.row_dict(db.q1("SELECT * FROM orders WHERE id=?", (oid,)), ("signal_snapshot", "risk_check"))
        if not o:
            raise HTTPException(404, "order not found")
        o["fills"] = _rows("SELECT * FROM fills WHERE order_id=? ORDER BY id", (oid,))
        return o
    return await run_in_threadpool(f)


@app.get("/api/fills", dependencies=[Depends(require_auth)])
async def fills(limit: int = Query(200, ge=1, le=500), symbol: str | None = None):
    if symbol:
        return await run_in_threadpool(_rows, "SELECT * FROM fills WHERE symbol=? ORDER BY id DESC LIMIT ?", (symbol, limit))
    return await run_in_threadpool(_rows, "SELECT * FROM fills ORDER BY id DESC LIMIT ?", (limit,))


@app.get("/api/candles", dependencies=[Depends(require_auth)])
async def candles(symbol: str = Query(min_length=1, max_length=30), limit: int = Query(400, ge=1, le=3000)):
    def f():
        tf = venues.VENUES[venues.venue_of(symbol)].timeframe
        b = store.load_bars(symbol, tf, limit=min(limit, 3000))
        return {"symbol": symbol, "timeframe": tf, "bars": [
            {"time": int(t), "open": o, "high": h, "low": l, "close": c, "volume": v}
            for t, o, h, l, c, v in zip(b["ts"].tolist(), b["open"].tolist(), b["high"].tolist(), b["low"].tolist(),
                                        b["close"].tolist(), b["volume"].tolist())],
            "quote": store.get_quote(symbol)}
    return await run_in_threadpool(f)


@app.get("/api/universe", dependencies=[Depends(require_auth)])
async def universe():
    return settings.get("universe")


@app.get("/api/llm_calls", dependencies=[Depends(require_operator)])
async def llm_calls(limit: int = Query(50, ge=1, le=500)):
    return await run_in_threadpool(_rows, "SELECT id, ts, purpose, model, served_model, input_tokens, output_tokens, "
                                   "cache_read_tokens, cost_usd, status, error FROM llm_calls ORDER BY id DESC LIMIT ?", (limit,))


@app.get("/api/llm_calls/{cid}", dependencies=[Depends(require_operator)])
async def llm_call(cid: int):
    r = await run_in_threadpool(db.q1, "SELECT * FROM llm_calls WHERE id=?", (cid,))
    if not r:
        raise HTTPException(404)
    d = dict(r)
    d["request"] = json.loads(d["request"]) if d["request"] else None
    return d


@app.get("/api/system", dependencies=[Depends(require_operator)])
async def system():
    def f():
        return {"settings": settings.all_settings(), "defaults": DEFAULT_SETTINGS,
                "gate_presets": GATE_PRESETS,
                "gate_profile": next((name for name, values in GATE_PRESETS.items()
                                      if settings.get("gates") == values), "custom"),
                "ceilings": {k: getattr(cfg, k) for k in ("ceiling_max_position_pct", "ceiling_max_gross_pct",
                                                          "ceiling_max_strategy_pct", "ceiling_max_order_usd",
                                                          "ceiling_monthly_llm_usd")},
                "bounds": _setting_bounds(),
                "jobs": _rows("SELECT * FROM jobs ORDER BY name"), "data_status": store.status_all(),
                "audit": _rows("SELECT * FROM audit_log ORDER BY id DESC LIMIT 50", (), ("detail",)),
                "risk_events": _rows("SELECT * FROM risk_events ORDER BY id DESC LIMIT 50", (), ("detail",)),
                "integrity": ledger.integrity_check(), "sandbox": isolation_report(),
                "llm": {"model": (li := llm_status(cfg))["label"], "provider": li["provider"], "effort": cfg.llm_effort,
                        "knowledge_cutoff": cfg.llm_knowledge_cutoff, "demo": li["demo"], "helpers": li["helpers"]}}
    return await run_in_threadpool(f)


# ------------------------------------------------------------------ operator actions
def _float_meta(key: str):
    try:
        value = db.get_meta(key)
        return float(value) if value is not None else None
    except ValueError:
        return None


def _setting_bounds() -> dict:
    """Allowed range per editable number, resolved against the hard ceilings, keyed "group.key"."""
    out = {}
    for path, (lo, hi) in SETTING_BOUNDS.items():
        if len(path) == 2:
            out[".".join(path)] = [settings._bound(0, lo), settings._bound(0, hi)]
    return out


def _audit(action: str, detail=None):
    db.ex("INSERT INTO audit_log(ts, actor, action, detail) VALUES(?,?,?,?)", (clock.now(), "operator", action, db.dumps(detail)))


@app.post("/api/ask", dependencies=[Depends(require_write)])
async def ask(body: AskBody):
    q = body.question.strip()
    if not q:
        raise HTTPException(400, "empty question")
    from ..agent.qa import ask as do_ask
    return await run_in_threadpool(do_ask, q)


@app.post("/api/ask/stream", dependencies=[Depends(require_write)])
async def ask_stream(body: AskBody):
    """Same as /api/ask, but streams each step as one JSON line, then the answer."""
    q = body.question.strip()
    if not q:
        raise HTTPException(400, "empty question")
    from ..agent.qa import ask as do_ask
    loop = asyncio.get_running_loop()
    events: asyncio.Queue = asyncio.Queue()
    t0 = time.monotonic()

    def progress(step: str, detail: str = ""):
        loop.call_soon_threadsafe(events.put_nowait, {"type": "step", "step": step, "detail": detail,
                                                      "t": round(time.monotonic() - t0, 2)})

    async def work():
        try:
            result = await run_in_threadpool(do_ask, q, progress)
            await events.put({"type": "answer", **result})
        except Exception as e:  # noqa: BLE001 - surfaced to the operator, not swallowed
            log.exception("ask failed")
            await events.put({"type": "error", "detail": str(e)[:300] or e.__class__.__name__})
        await events.put(None)

    task = asyncio.create_task(work())

    async def gen():
        try:
            while (ev := await events.get()) is not None:
                yield json.dumps(ev, default=str) + "\n"
        finally:
            await task

    return StreamingResponse(gen(), media_type="application/x-ndjson",
                             headers={"Cache-Control": "no-store", "X-Accel-Buffering": "no"})


@app.get("/api/questions", dependencies=[Depends(require_operator)])
async def questions(limit: int = Query(50, ge=1, le=200), before_id: int | None = Query(None, ge=1)):
    from ..agent.qa import history
    return await run_in_threadpool(history, limit, before_id)


@app.post("/api/control/{action}", dependencies=[Depends(require_write)])
async def control(action: str):
    extra: dict = {}

    def f():
        if action == "pause":
            settings.operator_update("agent", {"paused": True})
            journal.write("system", "Operator paused the agent", "No new research or new positions. Exits and risk "
                          "monitoring continue.", author="operator")
        elif action == "resume":
            settings.operator_update("agent", {"paused": False})
            journal.write("system", "Operator resumed the agent", "Research and trading schedules are active again.",
                          author="operator")
        elif action == "emergency_stop":
            settings.operator_update("agent", {"emergency_stop": True, "paused": True})
            n = oms.cancel_all("Emergency stop by operator.")
            db.set_meta("emergency_stop_at", str(clock.now()))
            db.set_meta("emergency_cancelled", str(n))
            extra["cancelled"] = n
            journal.write("risk", "EMERGENCY STOP", f"All agent trading halted; {n} open order(s) cancelled. Positions "
                          "are held. Use 'Flatten all' to close them.", author="operator")
        elif action == "clear_emergency":
            settings.operator_update("agent", {"emergency_stop": False})
            journal.write("system", "Emergency stop cleared", "The agent remains paused until resumed.", author="operator")
        elif action == "flatten_all":
            ids = []
            with db.tx():
                settings.operator_update("agent", {"paused": True})
                oms.cancel_all("Operator requested flatten-all.")
                for s in db.q("SELECT * FROM strategies"):
                    ids += live.flatten_strategy(dict(s), "Operator requested flatten-all.", origin="operator")
            journal.write("risk", "Operator: flatten all positions", f"{len(ids)} sell order(s) created; they fill at the "
                           "next usable quotes (NASDAQ orders wait for the session). The agent is paused to prevent "
                           "new entries; exits retry until flat.", author="operator")
        elif action == "reset_drawdown_halt":
            db.set_meta("drawdown_halt", "0")
            journal.write("system", "Drawdown halt reset by operator", "New positions are allowed again within limits.",
                          author="operator")
        elif action == "clear_system_halt":
            with db.tx():
                if not ledger.integrity_check()["ok"]:
                    raise HTTPException(409, "ledger still fails integrity checks; repair the mismatch before clearing")
                db.set_meta("system_halt", "0")
            journal.write("system", "System halt cleared by operator", "Ledger halt cleared.", author="operator")
        elif action.startswith("rejudge_") and action[8:].isdigit():
            db.set_meta("rejudge_requested", action[8:])
            journal.write("system", f"Operator asked to re-judge experiment {action[8:]}", "Its stored results are "
                          "re-checked against the current gates within ~15 seconds.", author="operator")
        elif action.startswith("probe_") and action[6:].isdigit():
            from ..agent.research_loop import probe_blocker
            e = db.q1("SELECT * FROM experiments WHERE id=?", (int(action[6:]),))
            why = probe_blocker(dict(e) if e else None, operator=True)
            if why:
                raise HTTPException(409, why)
            db.set_meta("probe_requested", action[6:])
            journal.write("system", f"Operator asked to paper-trade experiment {action[6:]} as a probe",
                          "The worker starts it within ~15 seconds; it trades from the next bar close.", author="operator",
                          refs={"experiment_id": int(action[6:])})
        elif action.startswith("research_"):
            venue = action.split("_", 1)[1].upper()
            if venue not in settings.get("universe"):
                raise HTTPException(400, "unknown venue")
            db.set_meta("research_requested", venue)
            journal.write("system", f"Operator requested a {venue} research cycle", "It starts within ~15 seconds if the "
                          "worker is running and budget allows.", author="operator")
        else:
            raise HTTPException(404, "unknown action")
        _audit(f"control.{action}")
        return {"ok": True, "controls": settings.get("agent"), **extra}
    return await run_in_threadpool(f)


@app.post("/api/strategies/{sid}/retire", dependencies=[Depends(require_write)])
async def retire(sid: int):
    def f():
        s = db.q1("SELECT * FROM strategies WHERE id=?", (sid,))
        if not s:
            raise HTTPException(404)
        live.retire_strategy(dict(s), "operator", origin="operator")
        journal.write("decision", f"Operator retired {s['name']} v{s['version']}", "Positions are being closed.",
                      refs={"strategy_id": sid}, author="operator")
        _audit("strategy.retire", {"id": sid})
        return {"ok": True}
    return await run_in_threadpool(f)


@app.post("/api/gate-presets/{name}", dependencies=[Depends(require_write)])
async def apply_gate_preset(name: str):
    if name not in GATE_PRESETS:
        raise HTTPException(400, "unknown gate preset")

    def f():
        merged = settings.operator_update("gates", GATE_PRESETS[name])
        journal.write("system", f"Operator selected {name} gate preset",
                      "Applies to future research decisions; existing allocations are unchanged.", author="operator")
        return merged
    return await run_in_threadpool(f)


@app.post("/api/settings", dependencies=[Depends(require_write)])
async def update_settings_batch(body: dict):
    """Save edits to several groups at once: every group is validated and written, or none is."""
    if not body or not all(isinstance(v, dict) for v in body.values()):
        raise HTTPException(400, "body must map settings groups to objects")
    if "agent" in body:
        raise HTTPException(400, "use /api/control for pause/stop")

    def f():
        merged = {}
        try:
            with db.tx():
                for group, values in body.items():
                    merged[group] = settings.operator_update(group, values)
        except settings.SettingsError as e:
            raise HTTPException(400, str(e))
        for group, values in body.items():
            journal.write("system", f"Operator changed {group} settings", json.dumps(values), author="operator")
        return merged
    return await run_in_threadpool(f)


@app.post("/api/settings/{group}", dependencies=[Depends(require_write)])
async def update_settings(group: str, body: dict):
    if group == "agent":
        raise HTTPException(400, "use /api/control for pause/stop")

    def f():
        try:
            merged = settings.operator_update(group, body)
        except settings.SettingsError as e:
            raise HTTPException(400, str(e))
        journal.write("system", f"Operator changed {group} settings", json.dumps(body), author="operator")
        return merged
    return await run_in_threadpool(f)


# ------------------------------------------------------------------ static UI
app.mount("/static", StaticFiles(directory=str(WEB)), name="static")


@app.get("/")
async def index():
    return FileResponse(WEB / "index.html")


def main():
    import uvicorn
    uvicorn.run(app, host=cfg.bind_host, port=cfg.bind_port,
                proxy_headers=bool(cfg.admin_password and cfg.trusted_proxies),
                forwarded_allow_ips=cfg.trusted_proxies)


if __name__ == "__main__":
    main()
