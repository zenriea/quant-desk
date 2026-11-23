"""Crypto keeps BACKFILL_DAYS of 15-minute history; older, shorter installs extend backwards once."""
from quantdesk.marketdata.synthetic import SyntheticProvider


class Counting(SyntheticProvider):
    def __init__(self):
        super().__init__("CRYPTO")
        self.calls = []

    def fetch_bars(self, symbols, timeframe, start, end):
        self.calls.append((tuple(symbols), start, end))
        return super().fetch_bars(symbols, timeframe, start, end)


def _first(sym):
    from quantdesk import db
    return db.q1("SELECT MIN(ts) t FROM bars WHERE symbol=? AND timeframe='15m'", (sym,))["t"]


def test_new_install_backfills_full_crypto_history(env):
    from quantdesk import clock
    from quantdesk.marketdata import ingest
    ingest.set_provider("CRYPTO", Counting())
    ingest.ingest_bars("CRYPTO", ["BTC-USD"])
    assert clock.now() - _first("BTC-USD") > (ingest.BACKFILL_DAYS["CRYPTO"] - 2) * 86400


def test_existing_one_year_history_is_extended_backwards_once_one_symbol_per_tick(env):
    from quantdesk import clock
    from quantdesk.marketdata import ingest
    p = Counting()
    ingest.set_provider("CRYPTO", p)
    full = (ingest.BACKFILL_DAYS["CRYPTO"] - 2) * 86400
    ingest.BACKFILL_DAYS["CRYPTO"], real = 90, ingest.BACKFILL_DAYS["CRYPTO"]
    try:
        ingest.ingest_bars("CRYPTO", ["BTC-USD", "ETH-USD"])   # an install with a shorter backfill
    finally:
        ingest.BACKFILL_DAYS["CRYPTO"] = real
    one_year = _first("BTC-USD")
    assert clock.now() - one_year < 91 * 86400
    ingest.ingest_bars("CRYPTO", ["BTC-USD", "ETH-USD"])
    extended = [s for s in ("BTC-USD", "ETH-USD") if clock.now() - _first(s) > full]
    assert len(extended) == 1                                  # one symbol per tick keeps the loop responsive
    ingest.ingest_bars("CRYPTO", ["BTC-USD", "ETH-USD"])
    assert all(clock.now() - _first(s) > full for s in ("BTC-USD", "ETH-USD"))
    n = len(p.calls)
    ingest.ingest_bars("CRYPTO", ["BTC-USD", "ETH-USD"])
    older = [c for c in p.calls[n:] if c[1] < one_year]
    assert older == []                                         # never re-downloads old history


def test_symbol_with_shorter_history_is_not_refetched_forever(env):
    from quantdesk import clock
    from quantdesk.marketdata import ingest

    class Young(Counting):
        def fetch_bars(self, symbols, timeframe, start, end):
            listed = int(clock.now() - 200 * 86400)            # listed 200 days ago
            out = super().fetch_bars(symbols, timeframe, max(start, listed), end)
            return {s: [b for b in v if b.ts >= listed] for s, v in out.items()}
    p = Young()
    ingest.set_provider("CRYPTO", p)
    ingest.ingest_bars("CRYPTO", ["SOL-USD"])
    ingest.ingest_bars("CRYPTO", ["SOL-USD"])
    n = len(p.calls)
    ingest.ingest_bars("CRYPTO", ["SOL-USD"])
    assert all(c[1] >= _first("SOL-USD") - 3 * 3600 for c in p.calls[n:])


def test_research_waits_until_history_extension_finishes(env):
    from quantdesk import settings, worker
    from quantdesk.marketdata import ingest
    settings.operator_update("universe", {"CRYPTO": ["BTC-USD", "ETH-USD", "SOL-USD"]})
    ingest.set_provider("CRYPTO", Counting())
    ingest.BACKFILL_DAYS["CRYPTO"], real = 90, ingest.BACKFILL_DAYS["CRYPTO"]
    try:
        ingest.ingest_bars("CRYPTO", ["BTC-USD", "ETH-USD", "SOL-USD"])
    finally:
        ingest.BACKFILL_DAYS["CRYPTO"] = real
    from quantdesk import clock
    assert ingest.history_pending("CRYPTO", ["BTC-USD", "ETH-USD", "SOL-USD"])
    assert worker.research_blocker("CRYPTO", clock.now()) == "waiting for market data"
    for _ in range(3):
        clock.advance(61)
        worker.bars_tick()           # pending extension makes the bar job run every minute, not every 15 min
    assert not ingest.history_pending("CRYPTO", ["BTC-USD", "ETH-USD", "SOL-USD"])
    assert worker.research_blocker("CRYPTO", clock.now()) is None


def test_history_extension_saves_chunks_and_resumes_after_a_failure(env):
    from quantdesk import clock
    from quantdesk.marketdata import ingest
    from quantdesk.marketdata.base import DataError

    class Flaky(Counting):
        fail_from = None

        def fetch_bars(self, symbols, timeframe, start, end):
            if self.fail_from is not None and len(self.calls) >= self.fail_from:
                raise DataError("rate limited")
            return super().fetch_bars(symbols, timeframe, start, end)
    p = Flaky()
    ingest.set_provider("CRYPTO", p)
    ingest.BACKFILL_DAYS["CRYPTO"], real = 90, ingest.BACKFILL_DAYS["CRYPTO"]
    try:
        ingest.ingest_bars("CRYPTO", ["BTC-USD"])
    finally:
        ingest.BACKFILL_DAYS["CRYPTO"] = real
    p.fail_from = len(p.calls) + 3          # the regular update, then two extension chunks, then an error
    ingest.ingest_bars("CRYPTO", ["BTC-USD"])
    reached = clock.now() - _first("BTC-USD")
    assert 90 * 86400 + ingest.EXTEND_CHUNK_DAYS * 86400 < reached < (real - 2) * 86400   # saved chunks kept
    assert ingest.history_pending("CRYPTO", ["BTC-USD"])
    p.fail_from = None
    n = len(p.calls)
    ingest.ingest_bars("CRYPTO", ["BTC-USD"])
    assert all(c[2] <= clock.now() - reached + 1 for c in p.calls[n + 1:])   # resumes, no re-download
    assert clock.now() - _first("BTC-USD") > (real - 2) * 86400
    assert not ingest.history_pending("CRYPTO", ["BTC-USD"])
