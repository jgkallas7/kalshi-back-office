"""Pins the P&L conventions.

The first two cases reproduce the shape of real closes that matched Kalshi's own
reported realized P&L to the cent (prices and sizes kept, tickers anonymized).
If a refactor breaks one of them, the engine no longer agrees with the
exchange: fix the engine, not the test.
"""
from conftest import make_fill

from back_office.pnl import compute_market


def test_long_yes_closed_by_a_no_side_fill():
    # Kalshi nets YES and NO: exiting a long YES prints as a fill on the NO side.
    fills = [make_fill("yes", 2967.88, 0.01, t="2026-01-02T00:00:00Z"),
             make_fill("no", 2967.88, 0.02, action="sell", t="2026-01-05T00:00:00Z")]
    m = compute_market("X", fills)
    assert abs(m.position) < 1e-9
    assert round(m.realized, 2) == 29.68          # (0.02 - 0.01) * 2967.88


def test_short_yes_closed_by_a_yes_side_fill():
    # Buying NO at yes-price 0.45 is short YES at 0.45; a YES-side fill at 0.03 closes it.
    fills = [make_fill("no", 9, 0.45, t="2026-01-02T00:00:00Z"),
             make_fill("yes", 9, 0.03, action="sell", t="2026-01-05T00:00:00Z")]
    m = compute_market("X", fills)
    assert abs(m.position) < 1e-9
    assert round(m.realized, 2) == 3.78           # (0.45 - 0.03) * 9


def test_fills_are_sorted_by_time_not_input_order():
    later = make_fill("no", 100, 0.30, t="2026-01-05T00:00:00Z")
    earlier = make_fill("yes", 100, 0.20, t="2026-01-02T00:00:00Z")
    assert round(compute_market("X", [later, earlier]).realized, 2) == 10.00


def test_average_cost_across_adds_and_partial_close():
    fills = [make_fill("yes", 100, 0.20, t="2026-01-01T00:00:00Z"),
             make_fill("yes", 300, 0.40, t="2026-01-02T00:00:00Z"),   # avg = 0.35
             make_fill("no", 200, 0.50, t="2026-01-03T00:00:00Z")]    # close 200 @ 0.50
    m = compute_market("X", fills)
    assert round(m.avg_cost, 4) == 0.35
    assert round(m.position, 2) == 200
    assert round(m.realized, 2) == 30.00           # (0.50 - 0.35) * 200


def test_flip_through_zero_opens_a_fresh_lot():
    fills = [make_fill("yes", 100, 0.20, t="2026-01-01T00:00:00Z"),
             make_fill("no", 250, 0.30, t="2026-01-02T00:00:00Z")]
    m = compute_market("X", fills)
    assert round(m.realized, 2) == 10.00           # only the 100 that were long
    assert round(m.position, 2) == -150
    assert m.avg_cost == 0.30                      # residual short entered at 0.30


def test_fees_are_kept_separate_from_gross():
    fills = [make_fill("yes", 100, 0.20, fee=0.50, t="2026-01-01T00:00:00Z"),
             make_fill("no", 100, 0.25, fee=0.40, t="2026-01-02T00:00:00Z")]
    m = compute_market("X", fills)
    assert round(m.realized, 2) == 5.00
    assert round(m.fees, 2) == 0.90
    assert round(m.realized_net, 2) == 4.10


def test_stored_cents_rows_are_read_by_field_name_not_magnitude():
    # A 1-cent fill stored in cents is `1`. A "price > 1.5 means cents" rule
    # would read it as $1.00 -- a 100x error on the cheapest contracts.
    row = {"side": "yes", "count": 83, "yes_price": 1, "fee_cost": 2,
           "created_time": "2026-01-01T00:00:00Z"}
    m = compute_market("X", [row])
    assert abs(m.avg_cost - 0.01) < 1e-12
    assert abs(m.fees - 0.02) < 1e-12

    decicent = {"side": "no", "count": 46.06, "yes_price": 5.8, "fee_cost": 4.418,
                "created_time": "2026-01-01T00:00:00Z"}
    m2 = compute_market("X", [decicent])
    assert abs(m2.avg_cost - 0.058) < 1e-12
    assert m2.position == -46.06


def test_unrealized_and_open_cost_basis():
    long = compute_market("L", [make_fill("yes", 100, 0.30)])
    assert round(long.unrealized(0.45), 2) == 15.00
    assert round(long.open_cost_basis, 2) == 30.00
    short = compute_market("S", [make_fill("no", 100, 0.70)])   # long NO at 0.30
    assert round(short.unrealized(0.60), 2) == 10.00
    assert round(short.open_cost_basis, 2) == 30.00
