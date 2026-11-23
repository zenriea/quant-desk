import numpy as np
import pytest

from conftest import put_bars


@pytest.mark.parametrize("venue,symbol", [("NASDAQ", "AAPL"), ("CRYPTO", "BTC-USD")])
def test_backtest_and_execution_share_causal_liquidity(env, venue, symbol):
    from quantdesk import clock
    from quantdesk.marketdata import store
    from quantdesk.research.backtest import liquidity_arrays
    from quantdesk.trading.execution import liquidity
    from quantdesk.venues import VENUES
    v = VENUES[venue]
    rng = np.random.default_rng(42)
    closes = 100 * np.exp(np.cumsum(rng.normal(0, 0.01, 2100)))
    put_bars(symbol, closes)
    panel = store.load_panel([symbol], v.timeframe)
    adv, sigma = liquidity_arrays(panel, venue)
    for cut in (10, 40, 600, 2000):
        clock.set_fixed(float(panel["ts"][cut]))  # bar cut has just opened, so must not be used
        a, s = liquidity(symbol, venue)
        assert a == pytest.approx(adv[cut, 0], rel=1e-10)
        if np.isfinite(sigma[cut, 0]):
            assert s == pytest.approx(sigma[cut, 0], rel=1e-10)
        else:
            assert s is None
    mutated = dict(panel, close=panel["close"].copy(), volume=panel["volume"].copy())
    mutated["close"][1000:] *= 100
    mutated["volume"][1000:] *= 100
    changed_adv, changed_sigma = liquidity_arrays(mutated, venue)
    np.testing.assert_allclose(changed_adv[:1001], adv[:1001], equal_nan=True)
    np.testing.assert_allclose(changed_sigma[:1001], sigma[:1001], equal_nan=True)


def test_shared_estimator_matches_direct_windows_with_gaps():
    from quantdesk.trading.liquidity import estimates
    rng = np.random.default_rng(2)
    close = np.exp(rng.normal(4, 0.1, (300, 2)))
    volume = rng.uniform(100, 1000, close.shape)
    close[50:60, 1] = np.nan
    adv, sigma = estimates(close, volume, 26, lookback=40)
    with np.errstate(invalid="ignore"):
        ret = np.vstack([np.full((1, 2), np.nan), np.log(close[1:] / close[:-1])])
    for t in (40, 70, 200, 300):
        sl = slice(t - 40, t)
        np.testing.assert_allclose(adv[t], np.nanmean(close[sl] * volume[sl], axis=0) * 26, rtol=1e-12)
        np.testing.assert_allclose(sigma[t], np.nanstd(ret[sl], axis=0, ddof=1) * np.sqrt(26), rtol=1e-12)


def test_insufficient_history_is_unknown_not_zero(env):
    from quantdesk.trading.execution import liquidity
    assert liquidity("AAPL", "NASDAQ") == (None, None)
    put_bars("AAPL", [100] * 5)
    assert liquidity("AAPL", "NASDAQ") == (None, None)


def test_backtest_exit_is_capped_by_participation():
    import numpy as np
    from quantdesk.research.backtest import run
    from quantdesk.research.strategy import Signals
    p = np.full((6, 1), 100.0)
    panel = dict(symbols=["BTC-USD"], open=p, close=p, ts=np.arange(6) * 900)  # 15-minute bar opens
    entry = np.zeros_like(p, dtype=bool)
    exit_ = entry.copy()
    entry[0] = exit_[2] = True
    adv = np.full_like(p, 1e7)
    adv[3] = 1000  # liquidity dries up on the exit bar
    costs = dict(backtest_half_spread_bps=0, base_slippage_bps=0, impact_coef=0, taker_fee_bps=0)
    res = run(dict(venue="CRYPTO", universe=["BTC-USD"], sizing=dict(weight=0.5)), panel,
              Signals(entry, exit_, None, p), costs, 0, 6, liquidity=(adv, np.ones_like(p)))
    assert len(res.trades) == 1 and res.trades[0]["exit_ts"] == 4 * 900  # remainder sold on the next liquid bar
    assert abs(res.trades[0]["pnl"]) < 1e-6
