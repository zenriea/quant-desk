"""Which bar is 'the latest closed bar' right now, and is our data current?

A bar is only ever used after it has closed. For NASDAQ bars we additionally wait
for the free-plan SIP delay (16 minutes) after the bar ends.
"""
from __future__ import annotations

from .. import venues
from ..venues import VENUES

SIP_DELAY = 16 * 60


def signal_delay_bars(venue: str) -> int:
    """Whole bars between a bar's close and the open that paper execution can first reach.

    NASDAQ: bar t is usable SIP_DELAY (16 min) after it closes, i.e. just after bar t+2 opens,
    so the backtest's earliest matching fill is the open of t+1+1. Crypto: the open of t+1."""
    if venue != "NASDAQ":
        return 0
    return SIP_DELAY // VENUES[venue].bar_seconds


def bar_closed(venue: str, ts: int, now: float) -> bool:
    """Bar has ended (and, for NASDAQ, passed the free-plan SIP delay). Session membership of
    NASDAQ bars is enforced at ingest (venues.in_regular_session)."""
    end = ts + VENUES[venue].bar_seconds
    return end + (SIP_DELAY if venue == "NASDAQ" else 0) <= now


def expected_last_bar(venue: str, now: float) -> int:
    """Open-time of the most recent bar that should be closed (and published) by ``now``."""
    step = VENUES[venue].bar_seconds
    if venue == "CRYPTO":
        return int(now // step) * step - step
    t = now - SIP_DELAY
    sess = venues.session_for(t)
    if sess is not None:
        o, c = venues.session_bounds(sess)
        if t >= o + step:
            return int(o + (min(t, c) - o) // step * step - step)
    return int(venues.last_close_before(t) - step)  # last bar of the previous session


def bars_current(venue: str, last_ts: int | None, now: float, tolerance_bars: int = 0) -> bool:
    if last_ts is None:
        return False
    # tolerance in bars of trading time: what was expected that many bars ago (skips nights/weekends)
    return last_ts >= expected_last_bar(venue, now - tolerance_bars * VENUES[venue].bar_seconds)


def quote_age(quote: dict | None, now: float) -> float | None:
    if not quote:
        return None
    return now - quote["ts"]
