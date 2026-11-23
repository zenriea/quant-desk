from conftest import make_strategy, put_bars


def _order(sid, bar, symbol, side, status="filled", price=None, ts=None):
    from quantdesk import db
    oid = db.ex("INSERT INTO orders(client_order_id, strategy_id, symbol, venue, side, qty, status, reason, rationale, "
                "signal_snapshot, origin, created_at, status_note) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (f"{sid}:{symbol}:{bar}:{side}", sid, symbol, "CRYPTO", side, 1.0, status, "entry" if side == "buy" else "exit rule",
                 "r", db.dumps({"bar": bar, "rule_values": ["close = 110 > 105 -> true"]}), "agent", bar + 960, "approved"))
    if price:
        db.ex("INSERT INTO fills(order_id, ts, symbol, side, qty, price, fee, price_basis, spread_bps) VALUES(?,?,?,?,?,?,?,?,?)",
              (oid, ts or bar + 1000, symbol, side, 1.0, price, 0.1, "coinbase quote", 4.0))
    return oid


def test_replay_lines_up_live_and_shadow_and_finds_the_first_divergence(env):
    from quantdesk import clock, db
    from quantdesk.agent.replay import replay
    from quantdesk.marketdata.freshness import expected_last_bar
    last = expected_last_bar("CRYPTO", clock.now())
    closes = [100.0] * 20 + [110, 110, 90, 90, 110, 110, 90, 90, 100, 100]
    start = last - (len(closes) - 1) * 900
    put_bars("BTC-USD", closes, start=start)
    put_bars("ETH-USD", [50.0] * len(closes), start=start)
    sid = make_strategy({"universe": ["BTC-USD"], "entry": "close > 105", "exit": "close < 95"}, allocation=1000,
                        venue="CRYPTO")
    bars = [start + i * 900 for i in range(len(closes))]
    db.ex("UPDATE strategies SET activated_at=?, created_at=? WHERE id=?", (bars[19], bars[19], sid))
    _order(sid, bars[20], "BTC-USD", "buy", price=110.2)     # matches the shadow entry
    _order(sid, bars[22], "BTC-USD", "sell", price=89.9)     # matches the shadow exit
    _order(sid, bars[23], "BTC-USD", "buy", status="rejected")  # live acted where the rules did not
    # no live order for the second shadow entry at bars[24]
    out = replay(dict(db.q1("SELECT * FROM strategies WHERE id=?", (sid,))))
    keys = [(r["bar"], r["side"], r["match"]) for r in out["rows"]]
    assert keys[:4] == [(bars[20], "buy", "both"), (bars[22], "sell", "both"), (bars[23], "buy", "live_only"),
                        (bars[24], "buy", "shadow_only")]
    first = out["rows"][out["first_divergence"]]
    assert first["bar"] == bars[23] and first["live"]["status"] == "rejected"
    assert first["rule_values_now"] and "false" in first["rule_values_now"][0]   # today's data says no entry
    both = out["rows"][0]
    assert both["shadow"]["fill"]["ts"] == bars[21]                             # next bar's open, crypto delay 0
    assert both["live"]["fill"]["px"] == 110.2 and both["usable_at"] == bars[20] + 900
    assert out["summary"]["matched"] == 2 and out["summary"]["live_only"] == 1


def test_replay_endpoint_is_public_read_and_404s(env, monkeypatch):
    import importlib
    monkeypatch.setenv("QD_ADMIN_PASSWORD", "correct horse")
    monkeypatch.setenv("QD_PUBLIC_VIEW", "1")
    from quantdesk.config import load_config
    load_config(reload=True)
    import quantdesk.api.app as appmod
    appmod = importlib.reload(appmod)
    from fastapi.testclient import TestClient
    client = TestClient(appmod.app)
    sid = make_strategy(venue="CRYPTO", spec_overrides={"universe": ["BTC-USD"]})
    r = client.get(f"/api/strategies/{sid}/replay")
    assert r.status_code == 200 and r.json()["rows"] == []
    assert client.get("/api/strategies/999/replay").status_code == 404


def _setup():
    from quantdesk import clock, db
    from quantdesk.marketdata.freshness import expected_last_bar
    last = expected_last_bar("CRYPTO", clock.now())
    closes = [100.0] * 20 + [110, 110, 90, 90, 110, 110, 90, 90, 100, 100]
    start = last - (len(closes) - 1) * 900
    put_bars("BTC-USD", closes, start=start)
    sid = make_strategy({"universe": ["BTC-USD"], "entry": "close > 105", "exit": "close < 95"}, allocation=1000,
                        venue="CRYPTO")
    bars = [start + i * 900 for i in range(len(closes))]
    db.ex("UPDATE strategies SET activated_at=?, created_at=? WHERE id=?", (bars[19], bars[19], sid))
    return sid, bars


def test_a_rejected_paper_buy_the_backtest_filled_is_a_divergence(env):
    from quantdesk import db
    from quantdesk.agent.replay import replay
    sid, bars = _setup()
    _order(sid, bars[20], "BTC-USD", "buy", status="rejected")   # same signal, the risk engine said no
    out = replay(dict(db.q1("SELECT * FROM strategies WHERE id=?", (sid,))))
    first = out["rows"][out["first_divergence"]]
    assert out["first_divergence"] == 0 and first["match"] == "fill_differs"
    assert first["shadow"]["fill"] and first["live"]["status"] == "rejected" and first["live"]["fill"] is None
    assert out["summary"]["fill_differs"] == 1 and out["summary"]["matched"] == 0


def test_replay_until_a_review_shows_only_what_was_known_then(env):
    from quantdesk import db
    from quantdesk.agent.replay import replay
    sid, bars = _setup()
    _order(sid, bars[20], "BTC-USD", "buy", price=110.2)
    _order(sid, bars[22], "BTC-USD", "sell", price=89.9)
    s = dict(db.q1("SELECT * FROM strategies WHERE id=?", (sid,)))
    # Bar 22 closed at bars[23]; paper's sell for it was created a minute later. A review in between
    # sees the backtest's exit but no paper order yet, and nothing after it.
    review_at = bars[23] + 30
    out = replay(s, until=review_at)
    assert [(r["bar"], r["side"], r["match"]) for r in out["rows"]] == [(bars[20], "buy", "both"),
                                                                       (bars[22], "sell", "shadow_only")]
    assert out["span"]["until"] == review_at and out["span"]["bars"] == 5
    early = replay(s, until=bars[22] + 300)                # bar 22 had not closed: its exit is not known yet
    assert [(r["bar"], r["side"]) for r in early["rows"]] == [(bars[20], "buy")]
    assert len(replay(s)["rows"]) > 2                       # without a cutoff, later bars appear
