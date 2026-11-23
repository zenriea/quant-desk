"""Venues and trading sessions.

NASDAQ uses the exchange_calendars XNAS calendar (holidays, early closes); only the
regular session is tradable. CRYPTO trades 24/7. New venues (e.g. NYSE) only need a
Venue entry with a calendar.
"""
from __future__ import annotations

import datetime as dt
from dataclasses import dataclass
from functools import lru_cache

import numpy as np
import pandas as pd

UTC = dt.timezone.utc


@dataclass(frozen=True)
class Venue:
    name: str
    asset_class: str          # "equity" | "crypto"
    timeframe: str            # research/trading bar size
    bar_seconds: int
    periods_per_year: float   # for annualisation
    qty_decimals: int         # 0 = whole shares
    bars_per_day: float


# "NASDAQ" is the US-equity venue: any Alpaca-listed stock or ETF (NYSE too), regular session only.
VENUES = {
    "NASDAQ": Venue("NASDAQ", "equity", "15m", 900, 252 * 26.0, 0, 26.0),
    "CRYPTO": Venue("CRYPTO", "crypto", "15m", 900, 365 * 96.0, 8, 96.0),
}


def venue_of(symbol: str) -> str:
    return "CRYPTO" if "-" in symbol or "/" in symbol else "NASDAQ"


def qty_decimals(venue: str, fractional_shares: bool = False) -> int:
    v = VENUES[venue]
    return 6 if (fractional_shares and v.asset_class == "equity") else v.qty_decimals


@lru_cache(maxsize=1)
def _xnas():
    import exchange_calendars as xc
    return xc.get_calendar("XNAS")


def _to_ts(epoch: float) -> pd.Timestamp:
    return pd.Timestamp(epoch, unit="s", tz="UTC")


def is_open(venue: str, epoch: float) -> bool:
    if venue == "CRYPTO":
        return True
    cal = _xnas()
    ts = _to_ts(epoch).floor("min")
    try:
        return bool(cal.is_trading_minute(ts))
    except Exception:
        return False


def session_for(epoch: float) -> pd.Timestamp | None:
    """The NASDAQ session date (midnight-naive) that contains epoch, else None."""
    cal = _xnas()
    date = _to_ts(epoch).tz_convert("America/New_York").normalize().tz_localize(None)
    if not cal.is_session(date):
        return None
    return date


def session_bounds(session: pd.Timestamp) -> tuple[float, float]:
    cal = _xnas()
    return (cal.session_open(session).timestamp(), cal.session_close(session).timestamp())


def next_open(epoch: float) -> float:
    """Epoch of the next NASDAQ regular-session open strictly after ``epoch`` (or now if open)."""
    cal = _xnas()
    ts = _to_ts(epoch)
    if is_open("NASDAQ", epoch):
        return epoch
    return cal.next_open(ts).timestamp()


def session_open_on_or_after(date: str) -> float:
    """Open of the NASDAQ session on ``date`` (YYYY-MM-DD), or of the next session if it is not one."""
    cal = _xnas()
    return cal.session_open(cal.date_to_session(pd.Timestamp(date), direction="next")).timestamp()


def next_close(epoch: float) -> float:
    """Epoch of the next NASDAQ regular-session close after ``epoch``."""
    return _xnas().next_close(_to_ts(epoch)).timestamp()


def last_close_before(epoch: float) -> float:
    cal = _xnas()
    return cal.previous_close(_to_ts(epoch)).timestamp()


def in_regular_session(ts: list[int], bar_seconds: int) -> np.ndarray:
    """Mask of intraday bars that lie wholly inside a NASDAQ regular session (drops pre/post-market)."""
    if not len(ts):
        return np.zeros(0, dtype=bool)
    t = pd.to_datetime(np.asarray(ts, dtype="int64"), unit="s", utc=True)
    days = t.tz_convert("America/New_York").tz_localize(None).normalize()
    sched = _xnas().schedule
    o = sched["open"].reindex(days).to_numpy(dtype="datetime64[ns]")
    c = sched["close"].reindex(days).to_numpy(dtype="datetime64[ns]")
    tv = t.tz_localize(None).to_numpy(dtype="datetime64[ns]")
    with np.errstate(invalid="ignore"):
        return (tv >= o) & (tv + np.timedelta64(bar_seconds, "s") <= c)  # NaT (no session) compares False


def session_label(bar_ts: int) -> str:
    return dt.datetime.fromtimestamp(bar_ts, UTC).strftime("%Y-%m-%d")


def describe_session(epoch: float) -> dict:
    """Human-facing status for the UI."""
    o = is_open("NASDAQ", epoch)
    info = {"NASDAQ": {"open": o}, "CRYPTO": {"open": True}}
    try:
        if o:
            sess = session_for(epoch)
            info["NASDAQ"]["closes_at"] = session_bounds(sess)[1]
        else:
            info["NASDAQ"]["opens_at"] = next_open(epoch)
    except Exception as e:  # calendar beyond range etc.
        info["NASDAQ"]["error"] = str(e)
    return info
