from __future__ import annotations

from dataclasses import dataclass


@dataclass
class Quote:
    symbol: str
    ts: float            # when the quote was observed (exchange time if known)
    bid: float | None
    ask: float | None
    bid_size: float | None = None
    ask_size: float | None = None
    last: float | None = None
    last_ts: float | None = None
    source: str = ""


@dataclass
class Bar:
    ts: int   # bar open time, epoch seconds UTC (NASDAQ daily: session date 00:00 ET)
    open: float
    high: float
    low: float
    close: float
    volume: float


@dataclass
class CashDividend:
    symbol: str
    ex_date: str            # YYYY-MM-DD; holders before that session's open are paid
    rate: float             # cash per share
    payable_date: str | None = None
    special: bool = False


class DataError(RuntimeError):
    """Raised when a provider cannot deliver data. Callers must stop dependent actions."""


class Provider:
    name = "base"
    venue = ""
    synthetic = False

    def fetch_bars(self, symbols: list[str], timeframe: str, start: int, end: int) -> dict[str, list[Bar]]:
        raise NotImplementedError

    def fetch_quotes(self, symbols: list[str]) -> dict[str, Quote]:
        raise NotImplementedError

    def fetch_cash_dividends(self, symbols: list[str], start: str, end: str) -> list[CashDividend]:
        """Cash dividends with an ex-date in [start, end] (YYYY-MM-DD). Venues without them return []."""
        return []
