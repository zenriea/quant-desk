"""Backtest fills never precede the data their signal depends on.

A NASDAQ 15-minute bar is published 16 minutes after it closes (free-plan SIP delay). A signal on it
can only fill at the first bar OPEN at or after that moment, in the shared backtest (research, shadow
review) and in the live replay alike.
"""
import numpy as np
import pytest

from conftest import put_bars

# 9:30 ET on Wednesday 2026-11-11 (EST, UTC-5), and on Friday 11-13 / Monday 11-16.
WED_OPEN = 1794407400
FRI_OPEN = WED_OPEN + 2 * 86400
MON_OPEN = FRI_OPEN + 3 * 86400
BAR = 900


def _session(open_ts):
    return [open_ts + i * BAR for i in range(26)]  # 9:30 ... 15:45 bar opens


def _panel(stamps, closes):
    n = len(stamps)
    c = np.array(closes, dtype=float).reshape(n, 1)
    o = np.vstack([c[:1], c[:-1]])
    return {"ts": np.array(stamps, dtype=np.int64), "symbols": ["AAPL"], "benchmark": None,
            "open": o, "high": np.maximum(o, c) * 1.001, "low": np.minimum(o, c) * 0.999, "close": c,
            "volume": np.full((n, 1), 1e9)}


def _first_fill(stamps, closes, signal_bar):
    from quantdesk.config import DEFAULT_SETTINGS
    from quantdesk.marketdata.freshness import signal_usable_after
    from quantdesk.research import backtest as B
    from quantdesk.research.strategy import compute_signals
    spec = {"name": "t", "venue": "NASDAQ", "universe": ["AAPL"], "kind": "dsl", "entry": "close > 105",
            "exit": "close < 95", "sizing": {"method": "fixed", "weight": 0.5}, "params": {}}
    panel = _panel(stamps, closes)
    events = []
    B.run(spec, panel, compute_signals(spec, {}, panel), DEFAULT_SETTINGS["costs"]["NASDAQ"], 0, len(stamps),
          usable_after=signal_usable_after("NASDAQ"), events=events)
    signal = next(e for e in events if e["kind"] == "signal")
    fill = next(e for e in events if e["kind"] == "fill")
    assert int(panel["ts"][signal["t"]]) == signal_bar
    return int(panel["ts"][fill["t"]])


def test_signal_usable_after_matches_data_latency():
    from quantdesk.marketdata.freshness import SIP_DELAY, signal_usable_after
    assert signal_usable_after("NASDAQ") == SIP_DELAY == 960
    assert signal_usable_after("CRYPTO") == 0


def test_a_stock_signal_fills_at_the_first_open_after_it_is_usable():
    # The 10:00 bar closes at 10:15 ET and is usable at 10:31. The 10:30 open is a minute too early:
    # the first eligible 15-minute open is 10:45.
    stamps = _session(WED_OPEN)
    closes = [100.0] * 26
    closes[2] = 110.0                                     # 10:00-10:15 bar triggers the entry
    fill = _first_fill(stamps, closes, signal_bar=WED_OPEN + 1800)
    usable = WED_OPEN + 1800 + BAR + 960                  # 10:31
    assert fill >= usable
    assert fill == WED_OPEN + 4500                        # 10:45, not 10:30


def test_a_session_close_signal_fills_at_the_next_trading_days_open():
    # The Friday 15:45 bar closes at 16:00, usable at 16:16 Friday. Nothing trades over the weekend:
    # the next eligible open is Monday 9:30, the first bar of the next session (not its second).
    stamps = _session(FRI_OPEN) + _session(MON_OPEN)
    closes = [100.0] * 52
    closes[25] = 110.0                                    # Friday's last bar triggers the entry
    fill = _first_fill(stamps, closes, signal_bar=FRI_OPEN + 25 * BAR)
    assert fill == MON_OPEN


def test_crypto_still_fills_at_the_next_open():
    from quantdesk.config import DEFAULT_SETTINGS
    from quantdesk.research import backtest as B
    from quantdesk.research.strategy import compute_signals
    spec = {"name": "t", "venue": "CRYPTO", "universe": ["AAPL"], "kind": "dsl", "entry": "close > 105",
            "exit": "close < 95", "sizing": {"method": "fixed", "weight": 0.5}, "params": {}}
    stamps = [WED_OPEN + i * BAR for i in range(10)]
    closes = [100.0] * 10
    closes[3] = 110.0
    panel = _panel(stamps, closes)
    events = []
    B.run(spec, panel, compute_signals(spec, {}, panel), DEFAULT_SETTINGS["costs"]["CRYPTO"], 0, 10, events=events)
    assert next(e["t"] for e in events if e["kind"] == "fill") == 4


def test_replay_shows_the_same_eligible_fill(env):
    from quantdesk import clock, db
    from quantdesk.agent.replay import replay
    from quantdesk.marketdata.base import Bar
    from quantdesk.marketdata.store import upsert_bars
    from conftest import make_strategy
    clock.set_fixed(WED_OPEN + 86400)
    stamps = _session(WED_OPEN)
    closes = [100.0] * 26
    closes[2] = 110.0
    opens = [closes[i - 1] if i else c for i, c in enumerate(closes)]
    upsert_bars("AAPL", "15m", [Bar(t, o, max(o, c) * 1.001, min(o, c) * 0.999, c, 1e9)
                                for t, o, c in zip(stamps, opens, closes)])
    sid = make_strategy({"entry": "close > 105", "exit": "close < 95"}, allocation=1000, venue="NASDAQ")
    db.ex("UPDATE strategies SET activated_at=?, created_at=? WHERE id=?", (WED_OPEN, WED_OPEN, sid))
    out = replay(dict(db.q1("SELECT * FROM strategies WHERE id=?", (sid,))))
    row = next(r for r in out["rows"] if r["bar"] == WED_OPEN + 1800)
    assert row["usable_at"] == WED_OPEN + 1800 + BAR + 960            # 10:31
    assert row["shadow"]["fill"]["ts"] == WED_OPEN + 4500             # 10:45
    assert row["shadow"]["fill"]["ts"] >= row["usable_at"]


@pytest.mark.parametrize("venue,universe,bench,usable_after", [
    ("NASDAQ", ["AAPL", "MSFT"], "QQQ", 960),
    ("CRYPTO", ["ETH-USD", "SOL-USD"], "BTC-USD", 0),
])
def test_experiment_runs_at_venue_timing_and_stresses_one_more_bar(env, monkeypatch, venue, universe, bench,
                                                                   usable_after):
    from quantdesk.config import DEFAULT_SETTINGS
    from quantdesk.research import experiment as E
    rng = np.random.default_rng(3)
    for s in universe + [bench]:
        put_bars(s, list(100 * np.exp(np.cumsum(rng.normal(0, 0.015, 900)))))
    calls = []
    orig = E.B.run

    def spy(*a, **kw):
        calls.append((kw.get("usable_after", 0), kw.get("delay", 0)))
        return orig(*a, **kw)
    monkeypatch.setattr(E.B, "run", spy)
    spec = {"name": "x", "venue": venue, "universe": universe, "kind": "dsl", "entry": "close > sma(close, n)",
            "exit": "close < sma(close, n)", "sizing": {"method": "fixed", "weight": 0.3}, "params": {"n": [20, 50]}}
    frozen = {}
    res = E.run_experiment(spec, DEFAULT_SETTINGS["costs"][venue], DEFAULT_SETTINGS["research"], bench, [],
                           "2026-06-01", on_freeze=frozen.update)
    assert {u for u, _ in calls} == {usable_after}
    assert [d for _, d in calls].count(1) == 2            # validation and test +1-bar stress only
    assert res["execution_timing"] == frozen["execution_timing"]
    assert res["execution_timing"]["version"] == 2 and res["execution_timing"]["usable_after_close_seconds"] == usable_after
    assert "execution_delay_bars" not in res


def test_new_experiments_store_their_fill_rule_and_old_records_are_untouched(env):
    from quantdesk import db
    from quantdesk.agent.research_loop import rejudge_experiment, research_cycle
    from test_agent import _seed_nasdaq
    _seed_nasdaq()
    research_cycle("NASDAQ")
    e = db.q1("SELECT id, test, leakage FROM experiments")
    leak = db.loads(e["leakage"])
    assert leak["execution_timing"]["version"] == 2 and leak["execution_timing"]["usable_after_close_seconds"] == 960
    # An experiment from before the change has no timing record; re-judging leaves its results as they ran.
    del leak["execution_timing"]
    db.ex("UPDATE experiments SET leakage=? WHERE id=?", (db.dumps(leak), e["id"]))
    db.ex("DELETE FROM strategies")
    rejudge_experiment(e["id"])
    after = db.q1("SELECT test, leakage FROM experiments WHERE id=?", (e["id"],))
    assert after["test"] == e["test"] and "execution_timing" not in db.loads(after["leakage"])
