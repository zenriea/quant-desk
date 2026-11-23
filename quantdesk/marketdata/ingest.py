"""Market-data ingestion with outage handling. Returns state *transitions* so the journal
reports "data went stale"/"data recovered" once, not every poll."""
from __future__ import annotations

import logging
import time

from .. import clock, db
from ..config import load_config
from ..venues import VENUES, in_regular_session
from . import store
from .base import DataError, Provider
from .freshness import bar_closed, bars_current

log = logging.getLogger(__name__)

# 15-minute bars. The sealed test is the last ~25% of history, and the deflated-PSR gate's power grows
# with the test window's CALENDAR length (Sharpe x sqrt(years)), not its bar count. One year of crypto
# left a ~3-month test that needed an annual Sharpe of ~2.5 to clear 20 trials; three years of crypto
# (~105k bars) and four years of stock sessions (~26k bars) give ~9-month and ~1-year tests.
# Installs with shorter history extend backwards one symbol per tick (_extend_history).
BACKFILL_DAYS = {"CRYPTO": 3 * 365, "NASDAQ": 4 * 365}
EXTEND_TOLERANCE = 7 * 86400  # history starting within a week of the target counts as complete
OVERLAP_SECONDS = {"CRYPTO": 3 * 3600, "NASDAQ": 4 * 86400}  # re-fetch recent stored bars to detect re-adjustment
BACKFILL_BUDGET_SEC = 120  # first downloads per call; the rest resume next tick so quotes/fills keep flowing
ADJUST_TOLERANCE = 0.003  # >0.3% change in an already-stored close = provider re-adjusted history
_providers: dict[str, Provider] = {}
_backoff: dict[str, tuple[float, float]] = {}  # key -> (retry_at, current_delay)


def _usable(venue: str, bars: list, now: float) -> list:
    """Closed, valid bars only; for equities also only regular-session bars (Alpaca returns pre/post-market).

    An invalid bar is dropped on its own. Rejecting the whole download instead would refetch
    the same bad bar on every refresh and leave the symbol stale for good."""
    bad = [b for b in bars if not store.valid_bar(b)]
    if bad:
        log.warning("dropped %d invalid %s bar(s), first at ts=%s", len(bad), venue, bad[0].ts)
        bars = [b for b in bars if store.valid_bar(b)]
    bars = [b for b in bars if bar_closed(venue, b.ts, now)]
    if venue == "NASDAQ" and bars:
        keep = in_regular_session([b.ts for b in bars], VENUES[venue].bar_seconds)
        bars = [b for b, k in zip(bars, keep) if k]
    return bars


def _blocked(key: str) -> bool:
    b = _backoff.get(key)
    return bool(b and clock.now() < b[0])


def _fail(key: str) -> None:
    """Circuit breaker: back off 30s, 60s, ... up to 15 minutes after provider failures."""
    delay = min((_backoff.get(key, (0, 15))[1]) * 2, 900)
    _backoff[key] = (clock.now() + delay, delay)


def _ok(key: str) -> None:
    _backoff.pop(key, None)


def get_provider(venue: str) -> Provider:
    if venue in _providers:
        return _providers[venue]
    cfg = load_config()
    if cfg.market_data_mode == "synthetic":
        from .synthetic import SyntheticProvider
        p: Provider = SyntheticProvider(venue)
    elif venue == "CRYPTO":
        from .coinbase import CoinbaseProvider
        p = CoinbaseProvider()
    elif venue == "NASDAQ":
        from .alpaca import AlpacaProvider
        p = AlpacaProvider(cfg.alpaca_key_id, cfg.alpaca_secret_key, cfg.alpaca_feed, cfg.alpaca_hist_feed)
    else:
        raise DataError(f"unknown venue {venue}")
    _providers[venue] = p
    return p


def set_provider(venue: str, provider: Provider | None) -> None:
    """Tests / demo wiring."""
    if provider is None:
        _providers.pop(venue, None)
    else:
        _providers[venue] = provider


def ingest_bars(venue: str, symbols: list[str]) -> list[dict]:
    """Fetch new closed bars. Returns transition events."""
    events = []
    tf = VENUES[venue].timeframe
    step = VENUES[venue].bar_seconds
    now = clock.now()
    try:
        provider = get_provider(venue)
    except DataError as e:
        for s in symbols:
            if store.set_status(f"bars:{s}", False, str(e)):
                events.append({"key": f"bars:{s}", "ok": False, "error": str(e), "symbol": s})
        return events
    starts = {}
    fetch_from = {}
    new = set()
    for s in symbols:
        last = store.last_bar_ts(s, tf)
        if last is None:
            new.add(s)
        starts[s] = (last + step) if last is not None else int(now - BACKFILL_DAYS[venue] * 86400)
        fetch_from[s] = (last - OVERLAP_SECONDS[venue]) if last is not None else starts[s]
    groups: dict[int, list[str]] = {}
    for s, st in fetch_from.items():
        if s not in new:
            groups.setdefault(st, []).append(s)
    # One request per symbol for crypto and for every first download; batched per start for
    # NASDAQ updates (a multi-symbol full backfill runs to thousands of pages)
    batches = [[s] for s in symbols if venue == "CRYPTO" or s in new] + \
        ([] if venue == "CRYPTO" else list(groups.values()))
    if _blocked(f"bars:{venue}"):
        return events
    began = time.monotonic()
    for batch in batches:
        if batch[0] in new and time.monotonic() - began > BACKFILL_BUDGET_SEC:
            continue
        st = min(fetch_from[s] for s in batch)
        try:
            if st < now:
                data = provider.fetch_bars(batch, tf, st, int(now))
            else:
                data = {s: [] for s in batch}
            _ok(f"bars:{venue}")
        except DataError as e:
            _fail(f"bars:{venue}")
            for s in batch:
                if store.set_status(f"bars:{s}", False, str(e)):
                    events.append({"key": f"bars:{s}", "ok": False, "error": str(e), "symbol": s})
            continue
        for s in batch:
            try:
                fresh = _usable(venue, data.get(s, []), now)
                adj = _detect_readjustment(s, tf, fresh)
                if adj is not None:
                    adjusted = _handle_readjustment(provider, venue, s, tf, adj, now)
                    events += adjusted
                    if any(not e["ok"] for e in adjusted):
                        store.set_status(f"bars:{s}", False, adjusted[0]["error"])
                        continue
                else:
                    store.upsert_bars(s, tf, [b for b in fresh if b.ts >= starts[s]])
            except DataError as e:
                if store.set_status(f"bars:{s}", False, str(e)):
                    events.append({"key": f"bars:{s}", "ok": False, "error": str(e), "symbol": s})
                continue
            last = store.last_bar_ts(s, tf)
            ok = bars_current(venue, last, now, tolerance_bars=2)
            err = None if ok else f"latest stored {tf} bar for {s} is older than expected"
            if store.set_status(f"bars:{s}", ok, err):
                events.append({"key": f"bars:{s}", "ok": ok, "error": err, "symbol": s})
    _extend_history(provider, venue, symbols, tf, now)
    return events


def history_pending(venue: str, symbols: list[str]) -> bool:
    """True while some symbol's history is still downloading: never fetched yet (unless its
    download already failed - a dead symbol must not block forever), or not yet extended to
    BACKFILL_DAYS."""
    tf = VENUES[venue].timeframe
    days = str(BACKFILL_DAYS[venue])
    for s in symbols:
        if store.last_bar_ts(s, tf) is None:
            st = db.q1("SELECT ok FROM data_status WHERE key=?", (f"bars:{s}",))
            if st is None or st["ok"]:
                return True
        elif db.get_meta(f"history_extended:{s}:{tf}") != days:
            return True
    return False


def _extend_history(provider: Provider, venue: str, symbols: list[str], tf: str, now: float) -> None:
    """Fetch older bars when stored history is shorter than BACKFILL_DAYS (e.g. an install made
    when the backfill was shorter). At most one symbol per call, and each symbol only once per
    target length, so the market loop stays responsive and young listings are not re-fetched."""
    days = BACKFILL_DAYS[venue]
    target = int(now - days * 86400)
    for s in symbols:
        key = f"history_extended:{s}:{tf}"
        if db.get_meta(key) == str(days):
            continue
        r = db.q1("SELECT MIN(ts) t FROM bars WHERE symbol=? AND timeframe=?", (s, tf))
        first = r["t"] if r else None
        if first is None:
            continue  # nothing stored yet: the regular backfill handles it
        if first > target + EXTEND_TOLERANCE:
            try:
                data = provider.fetch_bars([s], tf, target, int(first))
            except DataError as e:
                log.warning("history extension for %s failed: %s", s, e)
                return  # try again on a later tick
            store.upsert_bars(s, tf, [b for b in _usable(venue, data.get(s, []), now) if b.ts < first])
            db.set_meta(key, str(days))
            log.info("extended %s %s history back to %s days", s, tf, days)
            return
        db.set_meta(key, str(days))


def _detect_readjustment(symbol: str, tf: str, fresh: list) -> float | None:
    """Compare freshly fetched bars with the copies already stored. Returns the median
    ratio stored/fresh if history was re-adjusted (split or dividend), else None."""
    stored = store.stored_closes(symbol, tf, [b.ts for b in fresh])
    ratios = [stored[b.ts] / b.close for b in fresh if b.ts in stored and b.close > 0]
    if not ratios:
        return None
    ratios.sort()
    r = ratios[len(ratios) // 2]
    return r if abs(r - 1) > ADJUST_TOLERANCE else None


def _handle_readjustment(provider, venue, symbol, tf, ratio, now) -> list[dict]:
    """The provider changed already-stored history: replace it wholesale, and if the ratio
    is a stock split, adjust live positions so share counts match the new price basis."""
    try:
        full = provider.fetch_bars([symbol], tf, int(now - BACKFILL_DAYS[venue] * 86400), int(now)).get(symbol, [])
    except DataError as e:
        return [{"key": f"bars:{symbol}", "ok": False, "symbol": symbol,
                 "error": f"history was re-adjusted but the full re-download failed: {e}"}]
    full = _usable(venue, full, now)
    if not full:
        return [{"key": f"bars:{symbol}", "ok": False, "symbol": symbol,
                 "error": "history was re-adjusted but the replacement contained no closed bars"}]
    from ..trading import corporate
    # A crash must not commit the new price basis without the matching share adjustment.
    with db.tx():
        store.replace_bars(symbol, tf, full)
        return [corporate.on_history_readjusted(symbol, venue, ratio, len(full))]


def ingest_quotes(venue: str, symbols: list[str]) -> list[dict]:
    events = []
    if _blocked(f"quotes:{venue}"):
        return events
    try:
        provider = get_provider(venue)
        quotes = provider.fetch_quotes(symbols)
        err = None
        _ok(f"quotes:{venue}")
    except DataError as e:
        quotes, err = {}, str(e)
        _fail(f"quotes:{venue}")
    for s in symbols:
        q = quotes.get(s)
        symbol_error = err
        if q is not None:
            try:
                store.save_quote(venue, q)
                ok = True
            except DataError as invalid:
                ok, symbol_error = False, str(invalid)
        else:
            ok = False
        e = None if ok else (symbol_error or f"no quote returned for {s}")
        if store.set_status(f"quote:{s}", ok, e):
            events.append({"key": f"quote:{s}", "ok": ok, "error": e, "symbol": s})
    return events
