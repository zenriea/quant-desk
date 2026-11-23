"""Backtests start from the same signal-to-fill delay that paper trading has."""
import numpy as np
import pytest

from conftest import put_bars


def test_signal_delay_matches_data_latency():
    from quantdesk.marketdata.freshness import SIP_DELAY, signal_delay_bars
    from quantdesk.venues import VENUES
    # A NASDAQ bar is usable 16 min after it closes, just after the open two bars later.
    assert signal_delay_bars("NASDAQ") == 1
    assert VENUES["NASDAQ"].bar_seconds * 1 <= SIP_DELAY < VENUES["NASDAQ"].bar_seconds * 2
    assert signal_delay_bars("CRYPTO") == 0


@pytest.mark.parametrize("venue,universe,bench,base", [
    ("NASDAQ", ["AAPL", "MSFT"], "QQQ", 1),
    ("CRYPTO", ["ETH-USD", "SOL-USD"], "BTC-USD", 0),
])
def test_experiment_runs_at_venue_delay_and_stresses_one_more(env, monkeypatch, venue, universe, bench, base):
    from quantdesk.config import DEFAULT_SETTINGS
    from quantdesk.research import experiment as E
    rng = np.random.default_rng(3)
    for s in universe + [bench]:
        put_bars(s, list(100 * np.exp(np.cumsum(rng.normal(0, 0.015, 900)))))
    delays = []
    orig = E.B.run

    def spy(*a, **kw):
        delays.append(kw.get("delay", 0))
        return orig(*a, **kw)
    monkeypatch.setattr(E.B, "run", spy)
    spec = {"name": "x", "venue": venue, "universe": universe, "kind": "dsl", "entry": "close > sma(close, n)",
            "exit": "close < sma(close, n)", "sizing": {"method": "fixed", "weight": 0.3}, "params": {"n": [20, 50]}}
    frozen = {}
    res = E.run_experiment(spec, DEFAULT_SETTINGS["costs"][venue], DEFAULT_SETTINGS["research"], bench, [],
                           "2026-06-01", on_freeze=frozen.update)
    assert set(delays) == {base, base + 1}
    assert delays.count(base + 1) == 2  # validation and test delay stress only
    assert res["execution_delay_bars"] == base and frozen["execution_delay_bars"] == base
