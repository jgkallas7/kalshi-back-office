from conftest import make_fill

from back_office.fields import historical_settle_value, settle_yes_value
from back_office.pnl import compute_market


def test_long_residual_settles_at_one_dollar_when_yes_wins():
    m = compute_market("X", [make_fill("yes", 100, 0.30)], settle_yes_value=1.0)
    assert round(m.settled_pnl, 2) == 70.00
    assert m.position == 0 and m.avg_cost == 0 and m.settled


def test_long_residual_settles_at_zero_when_no_wins():
    m = compute_market("X", [make_fill("yes", 1000, 0.04, fee=0.70)], settle_yes_value=0.0)
    assert round(m.settled_pnl, 2) == -40.00
    assert round(m.realized_net, 2) == -40.70


def test_short_yes_residual_settles_in_our_favour_when_no_wins():
    m = compute_market("X", [make_fill("no", 300, 0.65)], settle_yes_value=0.0)
    assert round(m.settled_pnl, 2) == 195.00       # held NO bought at 0.35, paid $1
    assert m.position == 0


def test_partially_closed_then_settled():
    fills = [make_fill("yes", 500, 0.40, t="2026-01-01T00:00:00Z"),
             make_fill("no", 200, 0.47, t="2026-01-02T00:00:00Z")]
    m = compute_market("X", fills, settle_yes_value=1.0)
    assert round(m.realized, 2) == 14.00           # 200 closed at +0.07
    assert round(m.settled_pnl, 2) == 180.00       # 300 settled at +0.60
    assert round(m.realized_gross_incl_settlement, 2) == 194.00


def test_flat_market_settling_marks_settled_without_pnl():
    fills = [make_fill("yes", 10, 0.5, t="2026-01-01T00:00:00Z"),
             make_fill("no", 10, 0.6, t="2026-01-02T00:00:00Z")]
    m = compute_market("X", fills, settle_yes_value=0.0)
    assert m.settled and m.settled_pnl == 0.0


def test_settlement_value_parsing():
    assert settle_yes_value({"market_result": "yes"}) == 1.0
    assert settle_yes_value({"market_result": "no", "value": 0}) == 0.0
    assert settle_yes_value({"market_result": "scalar", "value": 37}) == 0.37
    assert settle_yes_value({"market_result": ""}) is None


def test_archive_settlement_value_only_when_final():
    assert historical_settle_value({"status": "finalized", "settlement_value_dollars": "1.0000"}) == 1.0
    assert historical_settle_value({"status": "settled", "settlement_value_dollars": "0.3700"}) == 0.37
    # not final yet, or no value: still open, never a silent 0.0
    assert historical_settle_value({"status": "active", "settlement_value_dollars": None}) is None
    assert historical_settle_value({"status": "finalized", "settlement_value_dollars": ""}) is None
