"""Background worker: the only process that trades, researches and writes market data.

Run:  python -m quantdesk.worker
Restart-safe: a DB lock with heartbeat prevents two workers; on start it reconciles
interrupted work; every trading action is idempotent (see oms.client_order_id).
"""
from __future__ import annotations

import logging
import os
import signal
import socket
import threading
import time
import traceback
import uuid

from . import clock, db, settings, venues
from .agent import journal
from .config import load_config
from .marketdata import freshness, ingest, store
from .trading import execution, ledger, live, mtm, oms, risk

log = logging.getLogger("quantdesk.worker")
LOCK_STALE = 30.0


class LockHeld(RuntimeError):
    pass


def acquire_lock(holder: str) -> float | None:
    """Take the single-worker lock. Returns the previous holder's last heartbeat (for
    downtime reporting) or None on first start. Raises LockHeld if another live worker owns it."""
    now = clock.now()
    with db.tx():
        r = db.q1("SELECT * FROM worker_lock WHERE id=1")
        if r and r["holder"] != holder and now - r["heartbeat_at"] < LOCK_STALE:
            raise LockHeld(f"worker {r['holder']} is alive (heartbeat {now - r['heartbeat_at']:.0f}s ago)")
        prev = r["heartbeat_at"] if r else None
        db.ex("INSERT INTO worker_lock(id, holder, heartbeat_at, started_at) VALUES(1,?,?,?) ON CONFLICT(id) DO UPDATE "
              "SET holder=excluded.holder, heartbeat_at=excluded.heartbeat_at, started_at=excluded.started_at",
              (holder, now, now))
    return prev


def heartbeat(holder: str) -> bool:
    n = db.connect().execute("UPDATE worker_lock SET heartbeat_at=? WHERE id=1 AND holder=?", (clock.now(), holder)).rowcount
    return n == 1


DIVIDEND_CHECK_SEC = 6 * 3600  # announced dividends change rarely; credits land on the ex-date


def job_due(name: str, every: float) -> bool:
    r = db.q1("SELECT last_started_at FROM jobs WHERE name=?", (name,))
    return r is None or r["last_started_at"] is None or clock.now() - r["last_started_at"] >= every


def run_job(name: str, fn, *args):
    now = clock.now()
    db.ex("INSERT INTO jobs(name, last_started_at, last_status, run_count) VALUES(?,?,'running',1) ON CONFLICT(name) DO UPDATE SET "
          "last_started_at=excluded.last_started_at, last_status='running', run_count=jobs.run_count+1", (name, now))
    try:
        out = fn(*args)
        db.ex("UPDATE jobs SET last_finished_at=?, last_status='ok', last_error=NULL WHERE name=?", (clock.now(), name))
        return out
    except Exception as e:
        log.error("job %s failed: %s", name, traceback.format_exc())
        db.ex("UPDATE jobs SET last_finished_at=?, last_status='error', last_error=? WHERE name=?",
              (clock.now(), f"{type(e).__name__}: {e}"[:1000], name))
        return None


# ------------------------------------------------------------------ recovery
def recover(prev_heartbeat: float | None) -> dict:
    ledger.init_account()
    now = clock.now()
    info = {"downtime": None}
    if prev_heartbeat:
        down = now - prev_heartbeat
        info["downtime"] = down
        if down > 120:
            journal.write("system", f"Back online after {down / 60:.0f} minutes offline",
                          f"The worker was not running from {time.strftime('%Y-%m-%d %H:%M UTC', time.gmtime(prev_heartbeat))}. "
                          "Nothing was traded while offline. Missing market data is being backfilled; signals for bars "
                          "that closed during the outage are not replayed. Orders that outlived their time-to-live expire.",
                          author="system", dedupe_key=f"restart:{int(prev_heartbeat)}")
    n = oms.recover_stuck_intents(max_age=0)
    if n:
        info["cancelled_intents"] = n
    for e in db.q("SELECT id, hypothesis_id FROM experiments WHERE status='running'"):
        db.ex("UPDATE experiments SET status='interrupted', error='worker restarted during the experiment', completed_at=? "
              "WHERE id=?", (now, e["id"]))
        db.ex("UPDATE hypotheses SET status='abandoned', updated_at=? WHERE id=? AND status='testing'", (now, e["hypothesis_id"]))
        journal.write("system", f"Experiment {e['id']} interrupted by a restart",
                      "It was not completed and will not be resumed half-way (that could mix data snapshots). "
                      "The idea can be re-tested as a new experiment.", refs={"experiment_id": e["id"]}, author="system",
                      dedupe_key=f"interrupted:{e['id']}")
    # Close worker-owned LLM calls and jobs killed mid-flight; leave API-owned 'question' calls untouched.
    db.ex("UPDATE llm_calls SET status='interrupted', error='worker restarted during the call' "
          "WHERE status='pending' AND purpose!='question'")
    for j in db.q("SELECT name, last_finished_at FROM jobs "
                  "WHERE last_started_at IS NOT NULL AND (last_finished_at IS NULL OR last_started_at > last_finished_at)"):
        db.ex("UPDATE jobs SET last_started_at=?, last_finished_at=?, last_status='interrupted', "
              "last_error='worker restarted while job was running' WHERE name=?",
              (j["last_finished_at"], now, j["name"]))
    # Prune orphaned data_status rows for symbols no longer tracked by the desk.
    tracked = {s for syms in market_symbols().values() for s in syms}
    for r in db.q("SELECT key FROM data_status"):
        if r["key"].split(":", 1)[-1] not in tracked:
            db.ex("DELETE FROM data_status WHERE key=?", (r["key"],))
    db.ex("DELETE FROM jobs WHERE name='research'")  # pre-0.2 shared research clock, replaced by research:<venue>
    info["retired_for_bar_size"] = retire_on_timeframe_change() + retire_stale_bar_size_strategies()
    from .agent.review import repair_drawdown_limits
    info["drawdown_limits_repaired"] = repair_drawdown_limits()
    chk = ledger.integrity_check()
    info["integrity"] = chk
    if not chk["ok"]:
        db.set_meta("system_halt", "1")
        journal.write("risk", "Ledger integrity check FAILED - new risk halted",
                      f"Recomputing from fills does not match stored balances: {chk}. New buys are blocked until the "
                      "operator investigates and clears the halt.", author="system")
    return info


def retire_on_timeframe_change() -> list[int]:
    """Strategy windows are counted in bars (sma(close, 200) meant 200 days on daily bars); after a
    bar-size change they would silently mean something else, so those strategies are closed out."""
    out = []
    for venue, v in venues.VENUES.items():
        key = f"timeframe:{venue}"
        if db.get_meta(key) == v.timeframe:
            continue
        for s in db.q("SELECT * FROM strategies WHERE venue=? AND status IN ('active','incubating')", (venue,)):
            s = dict(s)
            why = (f"{venue} now trades on {v.timeframe} bars; this strategy was tested on the previous bar size, "
                   "so its windows no longer mean what the test measured.")
            live.retire_strategy(s, "bar size changed", origin="system")
            journal.write("decision", f"Retired {s['name']} v{s['version']}: bar size changed",
                          why + " Its positions are being closed; research will test new ideas on the new bars.",
                          refs={"strategy_id": s["id"]}, author="system")
            out.append(s["id"])
        db.set_meta(key, v.timeframe)
    return out


def retire_stale_bar_size_strategies() -> list[int]:
    """Catches strategies the one-off check above missed: ones started after the bar-size change
    from an experiment that was run before it (e.g. a probe of an old daily-bar experiment)."""
    from .agent.research_loop import bar_size_mismatch
    out = []
    for s in db.q("SELECT s.*, e.windows AS exp_windows FROM strategies s JOIN experiments e ON e.id=s.experiment_id "
                  "WHERE s.status IN ('active','incubating')"):
        s = dict(s)
        why = bar_size_mismatch({"venue": s["venue"], "windows": s.pop("exp_windows")})
        if not why:
            continue
        live.retire_strategy(s, "tested on another bar size", origin="system")
        journal.write("decision", f"Retired {s['name']} v{s['version']}: tested on another bar size",
                      f"Experiment {s['experiment_id']} {why}. Its positions are being closed; research will test "
                      "new ideas on the current bars.", refs={"strategy_id": s["id"]}, author="system")
        out.append(s["id"])
    return out


# ------------------------------------------------------------------ jobs
def market_symbols() -> dict[str, list[str]]:
    """Research admission is not the same as the dependencies of an existing book."""
    uni = settings.get("universe")
    symbols = {s for values in uni.values() for s in values}
    symbols.update(settings.get("benchmarks").values())
    symbols.update(p["symbol"] for p in ledger.sleeve_positions())
    symbols.update(r["symbol"] for r in db.q(
        "SELECT DISTINCT symbol FROM orders WHERE status IN ('intent','submitted','partially_filled')"))
    for s in db.q("SELECT spec FROM strategies WHERE status IN ('active','incubating')"):
        symbols.update(db.loads(s["spec"], {}).get("universe", []))
    return {v: sorted(s for s in symbols if venues.venue_of(s) == v) for v in venues.VENUES}


def market_tick() -> None:
    """Quotes -> fills -> marks -> risk monitor."""
    uni = market_symbols()
    now = clock.now()
    for venue, syms in uni.items():
        if not syms:
            continue
        if venues.is_open(venue, now):
            need = True
        else:  # closed venue: refresh marks occasionally for display only
            need = job_due(f"closed_quotes:{venue}", 1800)
            if need:
                run_job(f"closed_quotes:{venue}", lambda: None)
        if need:
            _journal_data_events(ingest.ingest_quotes(venue, syms), "quote", venue)
    execution.process_orders()
    if job_due("mtm", 60):
        run_job("mtm", mtm.snapshot)
    for ev in risk.monitor():
        if ev["kind"] == "drawdown_halt":
            journal.write("risk", "Max drawdown reached - new risk halted",
                          f"Equity ${ev['equity']:,.0f} is {ev['drawdown']:.1%} below the peak ${ev['peak']:,.0f}. No new "
                          "positions until the operator resets the halt. Exits still run.", author="system")
        else:
            journal.write("risk", "Daily loss limit reached",
                          f"Equity is down {ev['loss']:.2%} today. No new positions until the next UTC day.", author="system")


def _journal_data_events(events: list[dict], what: str, venue: str) -> None:
    """One journal entry per venue and direction, not one per symbol."""
    down = [e for e in events if not e["ok"]]
    up = [e for e in events if e["ok"]]
    if down:
        syms = ", ".join(e["symbol"] for e in down)
        errs = sorted({(e["error"] or "")[:200] for e in down})
        journal.write("data", f"{venue} {what} data unavailable ({len(down)} symbol{'s' if len(down) > 1 else ''})",
                      f"Affected: {syms}. Reason: {' | '.join(errs)}. I am not trading these symbols or evaluating "
                      "signals that depend on them until the data recovers.", author="system")
    if up:
        journal.write("data", f"{venue} {what} data recovered ({len(up)} symbol{'s' if len(up) > 1 else ''})",
                      f"Current again: {', '.join(e['symbol'] for e in up)}. Dependent actions resume.", author="system")


def bars_tick() -> None:
    uni = market_symbols()
    sch = settings.get("schedule")
    now = clock.now()
    for venue, syms in uni.items():
        tf = venues.VENUES[venue].timeframe
        exp = freshness.expected_last_bar(venue, now)
        behind = [s for s in syms if (store.last_bar_ts(s, tf) or 0) < exp] or ingest.history_pending(venue, syms)
        key = f"bars:{venue}"
        if (behind and job_due(key + ":behind", 60)) or job_due(key, sch["bar_refresh_min"] * 60):
            run_job(key, lambda: None)
            run_job(key + ":behind", lambda: None)
            _journal_data_events(ingest.ingest_bars(venue, syms), "bar", venue)


def signals_tick() -> None:
    from .agent.research_loop import expire_probes
    now = clock.now()
    expire_probes()
    live.reconcile_flattening()
    live.activate_waiting()
    for s in db.q("SELECT * FROM strategies WHERE status='active'"):
        s = dict(s)
        exp = freshness.expected_last_bar(s["venue"], now)
        if (s["last_signal_bar"] or 0) >= exp:
            continue
        journal.set_status("trading", f"Evaluating {s['name']} v{s['version']} on the latest bar")
        run_job(f"signals:{s['id']}", live.evaluate_strategy, s)


class AnalystThread(threading.Thread):
    """Runs research and reviews one at a time, off the market loop."""

    def __init__(self, stop: threading.Event, ready: threading.Event):
        super().__init__(daemon=True, name="analyst")
        self.stop = stop
        self.ready = ready
        self.busy = False

    def run(self):
        while not self.stop.is_set() and not self.ready.wait(5):
            pass
        while not self.stop.is_set():
            try:
                self.tick()
            except Exception:
                log.error("analyst tick: %s", traceback.format_exc())
            self.stop.wait(15)

    def tick(self):
        from .agent.review import review_all
        sch = settings.get("schedule")
        now = clock.now()
        # daily review
        today = int(now // 86400)
        if time.gmtime(now).tm_hour >= sch["review_hour_utc"] and db.get_meta("last_review_day") != str(today):
            if db.q1("SELECT 1 FROM strategies WHERE status IN ('active','incubating')"):
                self.busy = True
                journal.set_status("reviewing", "Daily review of live strategies")
                run_job("review", review_all)
                self.busy = False
            db.set_meta("last_review_day", str(today))
        from .agent import research_loop
        probe = db.get_meta("probe_requested") or ""
        if probe:
            db.set_meta("probe_requested", "")
            run_job("probe", lambda: research_loop.start_probe(int(probe), origin="operator"))
        rejudge = db.get_meta("rejudge_requested") or ""
        if rejudge:
            db.set_meta("rejudge_requested", "")
            self.busy = True
            try:
                run_job("rejudge", lambda: research_loop.rejudge_experiment(int(rejudge)))
            finally:
                self.busy = False
        # research: each venue on its own clock
        requested = db.get_meta("research_requested") or ""
        if settings.get("agent").get("paused") and not requested:
            journal.set_status("paused", "Operator paused the agent: no new research or positions")
            return
        venue = requested if requested in settings.get("universe") else pick_research_venue(now)
        if requested:
            db.set_meta("research_requested", "")
        if venue is not None:
            self.busy = True
            try:
                run_research(venue)
            finally:
                self.busy = False
        rows = db.q("SELECT probe, COUNT(*) n FROM strategies WHERE status IN ('active','incubating') GROUP BY probe")
        n_probes = sum(r["n"] for r in rows if r["probe"])
        n_strats = sum(r["n"] for r in rows if not r["probe"])
        parts = ([f"{n_strats} live strateg{'y' if n_strats == 1 else 'ies'}"] if n_strats else []) + \
                ([f"{n_probes} probe{'' if n_probes == 1 else 's'} (forward test{'' if n_probes == 1 else 's'})"] if n_probes else [])
        live_n = n_strats + n_probes
        detail = (f"Monitoring {' and '.join(parts)} and the markets between research cycles"
                  if live_n else idle_detail(now))
        journal.set_status("waiting", detail, next_actions=next_actions())


def idle_detail(now: float) -> str:
    """Worker status when nothing is live; names the same top candidate as the pipeline and the UI verdict."""
    from .agent.research_loop import ranked_candidates
    best = next(iter(ranked_candidates(now - 7 * 86400)), None)
    if best and not best["bar_size_mismatch"]:
        if best["passed"] and not best["allocated"]:
            return (f"No strategy or probe is live. Experiment #{best['id']} passed every gate "
                    f"(test {best['test_return']:+.1%}) but was not allocated")
        if best["test_return"] > 0:
            return (f"No strategy or probe is live. Best idea #{best['id']} made {best['test_return']:+.1%} on its sealed "
                    "test" + (f" but cannot be probed: {best['probe_blocker']}" if best["probe_blocker"] else
                              "; a probe can start after the next research decision"))
    return ("No strategy or probe is live. No experiment on the current bar size has passed the gates or made money "
            "on its sealed test yet; the next research cycle keeps looking")


RESEARCH_VENUES = ("CRYPTO", "NASDAQ")
NOT_RUN = ("no_data", "budget", "llm_error")  # cycles that could not run keep their slot
LLM_RETRY_SEC = 600


def research_job(venue: str) -> str:
    return f"research:{venue}"


def _interval(venue: str) -> float:
    sch = settings.get("schedule")
    return sch["crypto_research_hours" if venue == "CRYPTO" else "nasdaq_research_hours"] * 3600


def _latest_bench_bar(venue: str) -> int:
    return store.last_bar_ts(settings.get("benchmarks")[venue], venues.VENUES[venue].timeframe) or 0


def research_due_at(venue: str, now: float) -> float:
    r = db.q1("SELECT last_started_at FROM jobs WHERE name=?", (research_job(venue),))
    at = r["last_started_at"] + _interval(venue) if r and r["last_started_at"] else now
    return max(at, float(db.get_meta(f"research_retry_after:{venue}") or 0))


def research_blocker(venue: str, now: float) -> str | None:
    """Why a venue cannot be researched right now regardless of its clock (None = it can)."""
    if not venue_ready(venue) or ingest.history_pending(venue, settings.get("universe")[venue]):
        return "waiting for market data"  # incl. a one-time history extension still downloading
    day0 = now - now % 86400
    n = db.q1("SELECT COUNT(*) n FROM experiments WHERE venue=? AND created_at>=?", (venue, day0))["n"]
    if n >= settings.get("schedule")["max_research_per_day"]:
        return "daily experiment limit reached"
    if _latest_bench_bar(venue) <= int(db.get_meta(f"research_bar:{venue}") or 0):
        return "waiting for a new bar"  # nothing new to look at since the last cycle
    return None


def pick_research_venue(now: float) -> str | None:
    due = [(research_due_at(v, now), v) for v in RESEARCH_VENUES
           if research_blocker(v, now) is None and research_due_at(v, now) <= now]
    return min(due)[1] if due else None


def run_research(venue: str) -> dict | None:
    from .agent import research_loop
    job = research_job(venue)
    prev = db.q1("SELECT last_started_at FROM jobs WHERE name=?", (job,))
    bar = _latest_bench_bar(venue)
    journal.set_status("researching", f"{venue}: starting a research cycle",
                       next_actions=[a for a in next_actions() if venue not in a["what"]])

    def cycle():
        try:
            return research_loop.research_cycle(venue)
        except Exception as e:
            research_loop.record_unexpected_failure(venue, e)
            raise
    res = run_job(job, cycle)
    status = res.get("status") if res else "exception"
    if status in NOT_RUN:
        db.ex("UPDATE jobs SET last_started_at=? WHERE name=?", (prev["last_started_at"] if prev else None, job))
        if status == "llm_error":
            db.set_meta(f"research_retry_after:{venue}", str(clock.now() + LLM_RETRY_SEC))
    else:
        db.set_meta(f"research_bar:{venue}", str(bar))
    return res


def venue_ready(venue: str, min_bars: int = 300) -> bool:
    bench = settings.get("benchmarks")[venue]
    tf = venues.VENUES[venue].timeframe
    r = db.q1("SELECT COUNT(*) n FROM bars WHERE symbol=? AND timeframe=?", (bench, tf))
    return r["n"] >= min_bars


def _next_research_at(venue: str, now: float) -> tuple[str, float]:
    block = research_blocker(venue, now)
    at = max(research_due_at(venue, now), now)
    if block == "waiting for market data":
        return f"{venue} research (waiting for market data)", now
    if block == "daily experiment limit reached":
        return f"Research cycle ({venue}, daily limit reached)", max(at, now - now % 86400 + 86400)
    if block == "waiting for a new bar":
        try:
            nxt = _next_bar_at(venue, now)
        except Exception:  # calendar out of range: show the clock-based time only
            nxt = at
        at = max(at, nxt)
    return f"Research cycle ({venue})", at


def _next_bar_at(venue: str, now: float) -> float:
    """When the next bar becomes usable (closed, plus the SIP delay for equities)."""
    step = venues.VENUES[venue].bar_seconds
    if venue == "CRYPTO":
        return (int(now // step) + 1) * step + 60
    if venues.is_open("NASDAQ", now):
        return (int(now // step) + 1) * step + freshness.SIP_DELAY + 60
    return venues.next_open(now) + step + freshness.SIP_DELAY + 60


def next_actions() -> list[dict]:
    sch = settings.get("schedule")
    now = clock.now()
    out = []
    for v in RESEARCH_VENUES:
        what, at = _next_research_at(v, now)
        out.append({"what": what, "at": at})
    out.append({"what": "Next crypto 15-minute bar, then signal evaluation", "at": _next_bar_at("CRYPTO", now),
                "venue": "CRYPTO", "kind": "signal"})
    try:
        if venues.is_open("NASDAQ", now):
            out.append({"what": "Next stock 15-minute bar, then signal evaluation", "at": _next_bar_at("NASDAQ", now),
                        "venue": "NASDAQ", "kind": "signal"})
        else:
            out.append({"what": "Stock market opens (queued orders become eligible)", "at": venues.next_open(now),
                        "venue": "NASDAQ", "kind": "market_open"})
    except Exception:
        pass
    day = now - now % 86400 + sch["review_hour_utc"] * 3600
    out.append({"what": "Daily strategy review", "at": day if day > now else day + 86400})
    return sorted(out, key=lambda x: x["at"])


def main() -> None:
    logging.basicConfig(level=os.environ.get("QD_LOG_LEVEL", "INFO"),
                        format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    cfg = load_config()
    db.init(cfg.db_path)
    holder = f"{socket.gethostname()}:{os.getpid()}:{uuid.uuid4().hex[:8]}"
    while True:
        try:
            prev = acquire_lock(holder)
            break
        except LockHeld as e:
            log.warning("%s; waiting", e)
            time.sleep(10)
    info = recover(prev)
    log.info("worker %s started; recovery: %s", holder, info)
    journal.write("system", "Worker started", f"Mode: {'DEMO rules analyst (no AI provider configured)' if cfg.demo_llm else 'AI analyst ' + cfg.llm_label}; "
                  f"market data: {cfg.market_data_mode}; NASDAQ data: {'Alpaca ' + cfg.alpaca_feed if cfg.alpaca_configured else 'NOT CONFIGURED'}.",
                  author="system")
    stop = threading.Event()

    def _sig(*_):
        log.info("shutdown requested")
        stop.set()
    signal.signal(signal.SIGTERM, _sig)
    signal.signal(signal.SIGINT, _sig)
    ready = threading.Event()
    analyst = AnalystThread(stop, ready)
    analyst.start()
    lost = threading.Event()

    def _beat():
        # Separate thread: slow network calls in the main loop can't make the lock look stale.
        while not stop.is_set():
            try:
                if not heartbeat(holder):
                    lost.set()
                    stop.set()
                    return
            except Exception:
                log.error("heartbeat failed: %s", traceback.format_exc())
            stop.wait(5)
    threading.Thread(target=_beat, daemon=True, name="heartbeat").start()
    last_tick = 0.0
    while not stop.is_set():
        if lost.is_set():
            log.error("lost the worker lock to another process; exiting")
            break
        sch = settings.get("schedule")
        if time.time() - last_tick >= sch["quote_poll_sec"]:
            last_tick = time.time()
            run_job("market_tick", market_tick)
            run_job("bars_tick", bars_tick)
            run_job("signals_tick", signals_tick)
            if job_due("dividends", DIVIDEND_CHECK_SEC):
                run_job("dividends", ingest.ingest_dividends)
            ready.set()
        stop.wait(2)
    analyst.join(timeout=30)
    db.ex("UPDATE worker_lock SET heartbeat_at=? WHERE id=1 AND holder=?", (clock.now() - LOCK_STALE, holder))
    log.info("worker stopped")


if __name__ == "__main__":
    main()
