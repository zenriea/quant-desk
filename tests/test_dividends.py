"""Cash dividends on paper positions: credited once, per sleeve, to holders before the ex-date."""
import datetime as dt
from fractions import Fraction

import pytest

from conftest import NASDAQ_OPEN_TS, make_strategy


def _session_after(epoch, n=1):
    """YYYY-MM-DD of the n-th NASDAQ session after the one containing ``epoch``, and its open."""
    from quantdesk import venues
    t = epoch
    for _ in range(n):
        t = venues.next_open(venues.next_close(t) + 60)
    return dt.datetime.fromtimestamp(t, dt.timezone.utc).astimezone(
        dt.timezone(dt.timedelta(hours=-5))).strftime("%Y-%m-%d"), t


def _trade(sid, side, qty, price, symbol="AAPL"):
    from quantdesk import clock, db
    from quantdesk.trading import ledger
    oid = db.ex("INSERT INTO orders(client_order_id, strategy_id, symbol, venue, side, qty, status, reason, rationale, "
                "origin, created_at) VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                (f"{sid}{side}{qty}{clock.now()}", sid, symbol, "NASDAQ", side, qty, "submitted", "t", "t", "agent",
                 clock.now()))
    ledger.apply_fill(dict(db.q1("SELECT * FROM orders WHERE id=?", (oid,))), qty, price, 0.0, {})


def _div(ex_date, rate=0.5, symbol="AAPL", special=False):
    from quantdesk.marketdata.base import CashDividend
    return CashDividend(symbol, ex_date, rate, None, special)


def test_dividend_credits_each_holding_sleeve_once_and_rebuilds(env):
    from quantdesk import clock, db
    from quantdesk.trading import corporate, ledger
    a, b = make_strategy(), make_strategy()
    clock.advance(600)
    _trade(a, "buy", 100, 200.0)
    _trade(b, "buy", 40, 200.0)
    ex, ex_open = _session_after(clock.now())
    clock.set_fixed(ex_open + 3600)
    _trade(b, "sell", 40, 199.0)       # sold after the ex-date open: still paid
    _trade(a, "buy", 10, 199.0)        # bought on the ex-date: these 10 shares are not paid
    cash0 = ledger.cash()
    r = corporate.apply_dividend(_div(ex, 0.5))
    assert r["credits"] == {str(a): pytest.approx(50.0), str(b): pytest.approx(20.0)}
    assert ledger.cash() == pytest.approx(cash0 + 70.0)
    assert corporate.apply_dividend(_div(ex, 0.5)) is None       # same ex-date again: no double credit
    assert ledger.cash() == pytest.approx(cash0 + 70.0)
    rows = {r["strategy_id"]: r["realized_pnl"] for r in db.q("SELECT * FROM positions WHERE symbol='AAPL'")}
    assert rows[b] == pytest.approx(-40.0 + 20.0)                  # -$1 x 40 on the sale, +$20 dividend
    assert ledger.integrity_check()["ok"]
    assert db.q1("SELECT COUNT(*) n FROM journal WHERE title LIKE 'AAPL: dividend%'")["n"] == 1


def test_dividend_waits_for_ex_date_and_ignores_pre_account_dates(env):
    from quantdesk import clock
    from quantdesk.trading import corporate, ledger
    sid = make_strategy()
    clock.advance(600)
    _trade(sid, "buy", 10, 100.0)
    ex, ex_open = _session_after(clock.now(), 2)
    assert corporate.apply_dividend(_div(ex)) is None              # announced, not ex yet
    clock.set_fixed(ex_open + 1)
    assert corporate.apply_dividend(_div(ex))["total"] == pytest.approx(5.0)
    assert corporate.apply_dividend(_div("2020-01-02")) is None     # before the paper account existed
    assert corporate.apply_dividend(_div(ex, rate=float("nan"))) is None
    assert ledger.integrity_check()["ok"]


def test_dividend_after_split_pays_post_split_shares(env):
    from quantdesk import clock
    from quantdesk.trading import corporate, ledger
    sid = make_strategy()
    clock.advance(600)
    _trade(sid, "buy", 10, 400.0)
    clock.advance(600)
    corporate.apply_split("AAPL", Fraction(4))
    ex, ex_open = _session_after(clock.now())
    clock.set_fixed(ex_open + 60)
    assert corporate.apply_dividend(_div(ex, 0.25))["total"] == pytest.approx(40 * 0.25)
    assert ledger.integrity_check()["ok"]


class DividendProvider:
    name, venue, synthetic = "fake", "NASDAQ", False

    def __init__(self, divs):
        self.divs = divs
        self.calls = []

    def fetch_cash_dividends(self, symbols, start, end):
        self.calls.append((tuple(symbols), start, end))
        return [d for d in self.divs if start <= d.ex_date <= end]


def test_ingest_checks_held_symbols_and_is_idempotent(env):
    from quantdesk import clock
    from quantdesk.marketdata import ingest
    from quantdesk.trading import ledger
    sid = make_strategy()
    clock.advance(600)
    _trade(sid, "buy", 10, 100.0)
    ex, ex_open = _session_after(clock.now())
    prov = DividendProvider([_div(ex, 1.0), _div(ex, 9.0, symbol="MSFT")])
    ingest.set_provider("NASDAQ", prov)
    clock.set_fixed(ex_open + 60)
    cash0 = ledger.cash()
    out = ingest.ingest_dividends()
    assert [r["symbol"] for r in out] == ["AAPL"] and prov.calls[0][0] == ("AAPL",)
    assert ingest.ingest_dividends() == []
    assert ledger.cash() == pytest.approx(cash0 + 10.0)


def test_alpaca_parses_cash_dividends(monkeypatch):
    from quantdesk.marketdata import alpaca
    pages = [{"corporate_actions": {"cash_dividends": [
                 {"symbol": "AAPL", "ex_date": "2026-11-10", "rate": 0.26, "payable_date": "2026-11-13", "special": False},
                 {"symbol": "AAPL", "ex_date": "2026-12-01", "rate": 0.26}]},  # outside the window
              "next_page_token": "p2"},
             {"corporate_actions": {"cash_dividends": [{"symbol": "PEP", "ex_date": "2026-11-05", "rate": "1.42",
                                                        "special": True}]}, "next_page_token": None}]
    seen = []

    def fake(url, params=None, headers=None, **kw):
        seen.append((url, dict(params)))
        return pages[len(seen) - 1]
    monkeypatch.setattr(alpaca, "get_json", fake)
    monkeypatch.setattr(alpaca.time, "sleep", lambda s: None)
    p = alpaca.AlpacaProvider("k", "s")
    out = p.fetch_cash_dividends(["AAPL", "PEP"], "2026-11-01", "2026-11-23")
    assert [(d.symbol, d.rate, d.special) for d in out] == [("AAPL", 0.26, False), ("PEP", 1.42, True)]
    assert seen[0][0].endswith("/v1/corporate-actions") and seen[0][1]["types"] == "cash_dividend"
    assert seen[1][1]["page_token"] == "p2"
