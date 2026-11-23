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


def test_units_reject_meaningless_limits():
    from quantdesk.units import drawdown_fraction
    assert [drawdown_fraction(v) for v in (0.05, -5, 25, 0, None, "5", True, 250, float("nan"))] == \
        [0.05, 0.05, 0.25, None, None, None, None, None, None]


def _admit(limit, test_max_drawdown=-0.04):
    """An analyst decision through strategy creation, the path the daily review then enforces."""
    from quantdesk.agent.research_loop import create_strategy
    spec = {"name": "adm", "venue": "NASDAQ", "universe": ["AAPL"], "kind": "dsl", "entry": "close > 0",
            "exit": "close < 0", "sizing": {"method": "fixed", "weight": 1.0}, "params": {}}
    res = {"test": {"max_drawdown": test_max_drawdown, "n_bars": 100}, "validation": {}, "windows": {},
           "chosen_params": {}}
    dec = {"allocation_pct": 0.2, "thesis": "t", "exit_plan": "e",
           "invalidation": {"max_live_drawdown_pct": limit, "review_after_days": 30, "min_trades_before_judging": 5,
                            "conditions": "c"}}
    prop = {"title": "x", "rationale": "r", "prediction": "p", "falsification": "f"}
    sid = create_strategy(1, 1, prop, spec, res, dec, "llm", None)
    s = strategy(sid)
    db.ex("INSERT INTO positions(strategy_id,symbol,venue,qty,avg_cost) VALUES(?,'AAPL','NASDAQ',?,100)",
          (sid, s["allocation"] / 100))
    put_quote("AAPL", 100, 100)
    mtm.snapshot()
    return sid


@pytest.mark.parametrize("limit, stored, down_to, retired", [
    (-3.0, 0.03, 96, True),      # written as a percentage: stored as 3%, a 4% loss retires
    (0.03, 0.03, 98, False),     # a 2% loss stays inside 3%
    (0, 0.06, 96, False),        # unusable: the rules engine's 1.5 x 4% = 6% applies
    (250, 0.06, 93, True),       # unusable: 6% applies, a 7% loss retires
])
def test_decision_to_creation_to_review_always_has_a_working_limit(env, limit, stored, down_to, retired):
    sid = _admit(limit)
    assert db.loads(strategy(sid)["invalidation"])["max_live_drawdown_pct"] == pytest.approx(stored)
    if limit in (0, 250):
        body = db.q1("SELECT body FROM journal WHERE kind='decision' ORDER BY id DESC LIMIT 1")["body"]
        assert "not a usable fraction" in body and "6.0%" in body
    put_quote("AAPL", down_to, down_to)
    assert (review_strategy(strategy(sid), analyst("continue"))["action"] == "retire") is retired
