from types import SimpleNamespace

import pytest

from conftest import make_strategy, put_quote
from quantdesk import db
from quantdesk.agent.review import live_stats, review_strategy
from quantdesk.trading import mtm


def sleeve_at_drawdown():
    sid = make_strategy(allocation=10000)
    db.ex("INSERT INTO positions(strategy_id,symbol,venue,qty,avg_cost) VALUES(?,'AAPL','NASDAQ',100,100)", (sid,))
    put_quote("AAPL", 120, 120)
    mtm.snapshot()
    db.close_thread_connection()  # the peak must survive a worker restart
    put_quote("AAPL", 105, 105)
    return sid


def strategy(sid):
    return dict(db.q1("SELECT * FROM strategies WHERE id=?", (sid,)))


def analyst(action):
    return SimpleNamespace(author="rules", review=lambda ctx: ({"action": action, "journal": "test"}, None))


def test_profitable_sleeve_retires_after_peak_loss(env):
    sid = sleeve_at_drawdown()
    db.ex("UPDATE strategies SET invalidation=? WHERE id=?", (db.dumps({"max_live_drawdown_pct": 0.10}), sid))
    assert live_stats(strategy(sid))["return"] == pytest.approx(0.05)
    result = review_strategy(strategy(sid), analyst("continue"))
    assert result["action"] == "retire"
    assert "-12.5%" in result["flags"][0]


def test_allocation_cut_preserves_drawdown(env):
    sid = sleeve_at_drawdown()
    review_strategy(strategy(sid), analyst("reduce"))
    assert strategy(sid)["allocation"] == 5000
    mtm.snapshot()
    assert live_stats(strategy(sid))["drawdown"] == pytest.approx(-0.125)


@pytest.mark.parametrize("stored", [0.03, -3.0, 3.0, -0.03])
def test_drawdown_limit_in_any_unit_retires_a_continue(env, stored):
    # The analyst wrote "-3.0" for a 3% limit on live strategies; compared with a fraction it read as 300%.
    sid = sleeve_at_drawdown()  # -12.5% from the peak
    db.ex("UPDATE strategies SET invalidation=? WHERE id=?", (db.dumps({"max_live_drawdown_pct": stored}), sid))
    result = review_strategy(strategy(sid), analyst("continue"))
    assert result["action"] == "retire"
    assert "-3.0%" in result["flags"][0]


def test_four_percent_loss_breaches_a_three_percent_limit(env):
    sid = make_strategy(allocation=10000)
    db.ex("INSERT INTO positions(strategy_id,symbol,venue,qty,avg_cost) VALUES(?,'AAPL','NASDAQ',100,100)", (sid,))
    put_quote("AAPL", 100, 100)
    mtm.snapshot()
    put_quote("AAPL", 96, 96)  # sleeve down 4% (400 / 10,000)
    db.ex("UPDATE strategies SET invalidation=? WHERE id=?", (db.dumps({"max_live_drawdown_pct": -3.0}), sid))
    assert live_stats(strategy(sid))["drawdown"] == pytest.approx(-0.04)
    assert review_strategy(strategy(sid), analyst("continue"))["action"] == "retire"


def test_stored_percent_limits_are_repaired_once(env):
    from quantdesk.agent.review import repair_drawdown_limits
    a, b = make_strategy(allocation=1000), make_strategy(allocation=1000)
    db.ex("UPDATE strategies SET invalidation=? WHERE id=?", (db.dumps({"max_live_drawdown_pct": -10.0, "conditions": "c"}), a))
    db.ex("UPDATE strategies SET invalidation=? WHERE id=?", (db.dumps({"max_live_drawdown_pct": 0.05}), b))
    assert repair_drawdown_limits() == [a]
    assert db.loads(strategy(a)["invalidation"]) == {"max_live_drawdown_pct": 0.1, "conditions": "c"}
    assert db.loads(strategy(b)["invalidation"]) == {"max_live_drawdown_pct": 0.05}
    assert repair_drawdown_limits() == []
    assert db.q1("SELECT COUNT(*) n FROM journal WHERE title LIKE 'Drawdown limit repaired%'")["n"] == 1


def test_units_reject_meaningless_limits():
    from quantdesk.units import drawdown_fraction
    assert [drawdown_fraction(v) for v in (0.05, -5, 25, 0, None, "5", True, 250, float("nan"))] == \
        [0.05, 0.05, 0.25, None, None, None, None, None, None]
