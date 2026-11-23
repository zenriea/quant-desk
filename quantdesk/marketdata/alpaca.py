"""Alpaca Market Data API v2 (stocks). Needs a (free) Alpaca account key pair.

Free plan: real-time quotes from the IEX feed only (a small share of consolidated volume,
so quotes can be wider or one-sided vs. the national best bid/offer); consolidated (SIP)
historical bars are available with a ~15-minute delay. Both limitations are disclosed in
the UI and applied in the simulator (see trading/execution.py).
"""
from __future__ import annotations

import datetime as dt
import time

from .base import Bar, CashDividend, DataError, Provider, Quote
from .http import get_json

BASE = "https://data.alpaca.markets"
TF = {"1d": "1Day", "1h": "1Hour", "15m": "15Min"}
MAX_PAGES = 1000  # loop guard only; pages hold ~1,000 bars whatever `limit` says


def _iso(epoch: int) -> str:
    return dt.datetime.fromtimestamp(epoch, dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _parse(s: str) -> float:
    s = s.replace("Z", "+00:00")
    if "." in s:  # trim nanoseconds to microseconds
        head, rest = s.split(".", 1)
        frac, tz = rest[:-6], rest[-6:]
        s = f"{head}.{frac[:6]}{tz}"
    return dt.datetime.fromisoformat(s).timestamp()


class AlpacaProvider(Provider):
    name = "alpaca"
    venue = "NASDAQ"

    def __init__(self, key_id: str, secret: str, feed: str = "iex", hist_feed: str = "sip"):
        if not (key_id and secret):
            raise DataError("Alpaca API keys are not configured (ALPACA_API_KEY_ID / ALPACA_API_SECRET_KEY)")
        self.headers = {"APCA-API-KEY-ID": key_id, "APCA-API-SECRET-KEY": secret}
        self.feed = feed
        self.hist_feed = hist_feed

    def fetch_bars(self, symbols, timeframe, start, end):
        # Free plan: SIP data must end >= 15 minutes ago.
        end = min(end, int(time.time()) - 16 * 60)
        out: dict[str, dict[int, Bar]] = {s: {} for s in symbols}
        params = {"symbols": ",".join(symbols), "timeframe": TF[timeframe], "start": _iso(start),
                  "end": _iso(end), "limit": 10000, "adjustment": "all", "feed": self.hist_feed}
        page = None
        for _ in range(MAX_PAGES):
            if page:
                params["page_token"] = page
            d = get_json(f"{BASE}/v2/stocks/bars", params=params, headers=self.headers)
            bars = d.get("bars") or {}
            if not isinstance(bars, dict):
                raise DataError(f"unexpected bars payload: {str(d)[:200]}")
            for sym, rows in bars.items():
                for b in rows or []:
                    t = int(_parse(b["t"]))
                    out.setdefault(sym, {})[t] = Bar(t, float(b["o"]), float(b["h"]), float(b["l"]),
                                                     float(b["c"]), float(b["v"]))
            page = d.get("next_page_token")
            if not page:
                break
            time.sleep(0.35)  # free plan: 200 requests/minute
        if page:  # never return a silently truncated history: later symbols would just look stale
            raise DataError(f"bar history for {len(symbols)} symbols exceeded {MAX_PAGES} pages; request fewer at once")
        return {s: [v[k] for k in sorted(v)] for s, v in out.items()}

    def fetch_quotes(self, symbols):
        d = get_json(f"{BASE}/v2/stocks/snapshots",
                     params={"symbols": ",".join(symbols), "feed": self.feed}, headers=self.headers,
                     timeout=5.0, retries=1)
        snaps = d.get("snapshots", d) if isinstance(d, dict) else {}
        out = {}
        for sym in symbols:
            s = snaps.get(sym)
            if not s:
                continue
            q = s.get("latestQuote") or {}
            tr = s.get("latestTrade") or {}
            try:
                out[sym] = Quote(
                    sym,
                    _parse(q["t"]) if q.get("t") else time.time(),
                    float(q["bp"]) if q.get("bp") else None,
                    float(q["ap"]) if q.get("ap") else None,
                    float(q.get("bs") or 0), float(q.get("as") or 0),
                    float(tr["p"]) if tr.get("p") else None,
                    _parse(tr["t"]) if tr.get("t") else None,
                    f"alpaca-{self.feed}")
            except (KeyError, TypeError, ValueError) as e:
                raise DataError(f"bad snapshot for {sym}: {e}")
        return out

    def fetch_cash_dividends(self, symbols, start, end):
        """Announced cash dividends from Alpaca's corporate-actions endpoint (market data API)."""
        params = {"symbols": ",".join(symbols), "types": "cash_dividend", "start": start, "end": end, "limit": 1000}
        out: list[CashDividend] = []
        page = None
        for _ in range(MAX_PAGES):
            if page:
                params["page_token"] = page
            d = get_json(f"{BASE}/v1/corporate-actions", params=params, headers=self.headers)
            actions = d.get("corporate_actions") if isinstance(d, dict) else None
            if not isinstance(actions, dict):
                raise DataError(f"unexpected corporate-actions payload: {str(d)[:200]}")
            for r in actions.get("cash_dividends") or []:
                try:
                    out.append(CashDividend(str(r["symbol"]), str(r["ex_date"]), float(r["rate"]),
                                            r.get("payable_date"), bool(r.get("special"))))
                except (KeyError, TypeError, ValueError) as e:
                    raise DataError(f"bad cash dividend record {str(r)[:200]}: {e}")
            page = d.get("next_page_token")
            if not page:
                break
            time.sleep(0.35)
        if page:
            raise DataError(f"corporate actions exceeded {MAX_PAGES} pages")
        return [x for x in out if start <= x.ex_date <= end]
