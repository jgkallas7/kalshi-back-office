from conftest import make_fill

from back_office import reconcile as rc
from back_office.reconcile import Snapshot, reconcile

TK = "KXEXAMPLE-26JAN01-A"


AS_OF = 1_767_225_600
YEAR = 365 * 86_400


def snap(fills=(), positions=(), settlements=(), balance_cents=0, pv_cents=0,
         deposits=(), withdrawals=(), historical_markets=()):
    return Snapshot(as_of=AS_OF, balance_cents=balance_cents,
                    portfolio_value_cents=pv_cents, deposits=list(deposits),
                    withdrawals=list(withdrawals), positions=list(positions),
                    fills=list(fills), settlements=list(settlements),
                    historical_markets=list(historical_markets))


def position(ticker, pos, realized, fees=None):
    p = {"ticker": ticker, "position_fp": f"{pos:.2f}", "realized_pnl_dollars": f"{realized:.4f}"}
    if fees is not None:
        p["fees_paid_dollars"] = f"{fees:.4f}"
    return p


def kinds(result):
    return sorted(b.kind for b in result.breaks)


def funded(amount_cents, created_ts=None):
    d = {"amount_cents": amount_cents, "status": "applied"}
    if created_ts is not None:
        d["created_ts"] = created_ts
    return [d]


# ---- regression: positions held to settlement --------------------------------

def test_position_held_to_settlement_ties_once_settlement_is_applied():
    """Regression. Kalshi zeroes a position when its market settles. An engine
    that rebuilds positions from fills alone never sees that, so every market
    held to expiry showed up as `engine 10,000.00 vs Kalshi 0.00`. The engine
    must apply the settlement before comparing."""
    fills = [make_fill("yes", 10_000, 0.01, fee=1.00, ticker=TK)]
    settled = [{"ticker": TK, "market_result": "no", "value": 0}]
    venue = [position(TK, 0, -100.00, fees=1.00)]     # lost $100 at settlement
    # $1,000 in, $101 lost (100 at settlement + $1 fee) -> $899 cash
    r = reconcile(snap(fills, venue, settled, balance_cents=89_900, deposits=funded(100_000)))
    assert r.breaks == []
    assert r.markets[TK].settled


def test_without_the_settlement_record_the_residual_is_flagged():
    fills = [make_fill("yes", 10_000, 0.01, ticker=TK)]
    r = reconcile(snap(fills, [position(TK, 0, -100.00)], settlements=[]))
    pm = [b for b in r.breaks if b.kind == "position_mismatch"]
    assert len(pm) == 1 and "engine 10,000.00 vs Kalshi 0.00" in pm[0].detail


def test_realized_comparison_follows_the_venue_definition(monkeypatch):
    fills = [make_fill("yes", 100, 0.30, ticker=TK)]
    settled = [{"ticker": TK, "market_result": "yes"}]
    # venue excludes the settlement payout from realized P&L
    monkeypatch.setattr(rc, "VENUE_REALIZED_INCLUDES_SETTLEMENT", False)
    r = reconcile(snap(fills, [position(TK, 0, 0.0)], settled))
    assert "realized_mismatch" not in kinds(r)
    # venue includes it
    monkeypatch.setattr(rc, "VENUE_REALIZED_INCLUDES_SETTLEMENT", True)
    r = reconcile(snap(fills, [position(TK, 0, 70.0)], settled))
    assert "realized_mismatch" not in kinds(r)


# ---- each break type ---------------------------------------------------------

def test_clean_open_position_has_no_breaks():
    fills = [make_fill("yes", 100, 0.30, fee=0.25, ticker=TK)]
    venue = [position(TK, 100, 0.0, fees=0.25)]
    # $100 in, $30 + $0.25 spent, position marked at $0.30 -> no P&L but the fee
    r = reconcile(snap(fills, venue, balance_cents=6_975, pv_cents=3_000, deposits=funded(10_000)))
    assert r.breaks == []
    assert round(r.cash_pnl, 2) == -0.25
    assert abs(r.cash_identity_residual) < 1e-9


def test_position_mismatch_from_a_missing_fill():
    fills = [make_fill("yes", 200, 0.10, ticker=TK),
             make_fill("no", 120, 0.15, ticker=TK, t="2026-01-02T00:00:00Z")]
    r = reconcile(snap(fills, [position(TK, 50, 6.0)]))
    assert kinds(r) == ["position_mismatch"]
    b = next(b for b in r.breaks if b.kind == "position_mismatch")
    assert b.magnitude == 30


def test_realized_mismatch():
    fills = [make_fill("yes", 100, 0.20, ticker=TK),
             make_fill("no", 100, 0.25, ticker=TK, t="2026-01-02T00:00:00Z")]
    r = reconcile(snap(fills, [position(TK, 0, 4.00)]))
    b = next(b for b in r.breaks if b.kind == "realized_mismatch")
    assert round(b.magnitude, 2) == 1.00


def test_fee_mismatch():
    fills = [make_fill("yes", 100, 0.20, fee=0.50, ticker=TK)]
    r = reconcile(snap(fills, [position(TK, 100, 0.0, fees=0.75)]))
    assert "fee_mismatch" in kinds(r)


def test_missing_fills_for_an_open_position():
    r = reconcile(snap(positions=[position("KXEXAMPLE-26JAN01-Z", 25, 0.0)]))
    assert kinds(r) == ["missing_fills"]


def test_flat_market_without_fills_is_not_a_break():
    r = reconcile(snap(positions=[position("KXEXAMPLE-26JAN01-Z", 0, 12.0)]))
    assert r.breaks == []


def test_fill_without_a_side_is_flagged_not_guessed():
    f = make_fill("yes", 10, 0.5, ticker=TK)
    f.pop("side"), f.pop("outcome_side")
    r = reconcile(snap([f]))
    assert kinds(r) == ["unknown_side"]


# ---- cash view ---------------------------------------------------------------

def test_only_applied_funding_counts():
    deposits = [{"amount_cents": 100_000, "status": "applied"},
                {"amount_cents": 50_000, "status": "pending"}]
    withdrawals = [{"amount_cents": 20_000, "status": "applied"}]
    r = reconcile(snap(balance_cents=80_000, deposits=deposits, withdrawals=withdrawals))
    assert r.net_deposits == 800.00
    assert r.cash_pnl == 0.0
    assert r.n_deposits == 1 and r.n_withdrawals == 1


def test_cash_identity_break_when_cash_does_not_tie():
    # the engine says nothing happened, but $50 is missing from cash
    r = reconcile(snap(balance_cents=95_000, deposits=funded(100_000)))
    assert kinds(r) == ["cash_identity"]
    assert round(r.cash_identity_residual, 2) == -50.00


# ---- regression: markets that settled before the live window ------------------

def test_market_settled_before_the_live_window_closes_from_the_archive():
    """Regression. Kalshi's settlements endpoint only covers recent markets, and
    its positions endpoint drops old ones entirely. On a live account, thousands of
    old markets never closed in the engine, and the cash identity was far off
    with no per-market break to say why."""
    old = "KXEXAMPLE-25JUN01-OLD"
    fills = [make_fill("yes", 100, 0.30, ticker=old)]
    archive = [{"ticker": old, "status": "finalized", "settlement_value_dollars": "1.0000"}]
    # $100 in, bought 100 YES @ 0.30, won $1 each -> $170 cash, nothing open
    r = reconcile(snap(fills, balance_cents=17_000, deposits=funded(10_000),
                       historical_markets=archive))
    assert r.breaks == [] and r.markets[old].settled
    # without the archive the engine thinks the $30 is still tied up in an open lot
    r = reconcile(snap(fills, balance_cents=17_000, deposits=funded(10_000)))
    assert not r.markets[old].settled
    assert round(r.cash_identity_residual, 2) == 100.00


def test_live_settlement_wins_over_the_archive():
    fills = [make_fill("yes", 10, 0.50, ticker=TK)]
    live = [{"ticker": TK, "market_result": "yes"}]
    archive = [{"ticker": TK, "status": "finalized", "settlement_value_dollars": "0.0000"}]
    r = reconcile(snap(fills, settlements=live, historical_markets=archive))
    assert round(r.markets[TK].settled_pnl, 2) == 5.00


# ---- cash identity: non-trading cash ------------------------------------------

def test_interest_sized_residual_is_reported_not_a_break():
    # $1,000 funded a year ago, $35 of interest credited, no trades
    r = reconcile(snap(balance_cents=103_500, deposits=funded(100_000, AS_OF - YEAR)))
    assert r.breaks == []
    assert round(r.cash_identity_residual, 2) == 35.00


def test_more_than_interest_could_explain_is_a_break():
    # 6% of $1,000 for a year is $60; $500 unexplained is missing settlements or fills
    r = reconcile(snap(balance_cents=150_000, deposits=funded(100_000, AS_OF - YEAR)))
    assert kinds(r) == ["cash_identity"]
    assert "more than interest could explain" in r.breaks[0].detail


def test_residual_jump_between_runs_is_a_break():
    deps = funded(100_000, AS_OF - YEAR)
    assert reconcile(snap(balance_cents=103_600, deposits=deps), prev_residual=35.0).breaks == []
    r = reconcile(snap(balance_cents=103_500, deposits=deps), prev_residual=0.0)
    assert kinds(r) == ["cash_drift"]


def test_drift_tolerance_allows_a_month_of_interest_on_a_large_balance():
    assert rc.drift_tolerance(500.0) == rc.CASH_DRIFT_FLOOR
    assert round(rc.drift_tolerance(13_000.0), 2) == 65.00      # one month at 6%
    # a $40 monthly interest credit on $13k is not a break
    deps = funded(1_300_000, AS_OF - YEAR)
    r = reconcile(snap(balance_cents=1_304_000, deposits=deps), prev_residual=0.0)
    assert "cash_drift" not in kinds(r)


# ---- re-check before reporting ----------------------------------------------------

class FreshClient:
    """Single-market lookups as they look a few seconds after the snapshot."""
    def __init__(self, positions, fills=(), settlements=()):
        self.positions, self.fills, self.settlements = positions, list(fills), list(settlements)

    def get_positions(self, ticker=None):
        return [p for p in self.positions if p["ticker"] == ticker]

    def get_fills(self, ticker=None, include_historical=True):
        return [f for f in self.fills if f["ticker"] == ticker]

    def get_settlements(self, ticker=None):
        return [s for s in self.settlements if s["ticker"] == ticker]


def test_break_from_a_fill_that_landed_mid_snapshot_clears_on_recheck():
    # the positions call already saw a second fill that the fills call missed
    first = make_fill("yes", 100, 0.30, ticker=TK, fill_id="f1")
    second = make_fill("yes", 50, 0.32, ticker=TK, fill_id="f2", t="2026-01-02T00:00:00Z")
    s = snap([first], [position(TK, 150, 0.0)], deposits=funded(3_000))   # $30 now in the lot
    r = reconcile(s)
    assert kinds(r) == ["position_mismatch"]
    r = rc.recheck(FreshClient([position(TK, 150, 0.0)], [first, second]), s, r)
    assert r.breaks == [] and r.rechecked_clean == 1


def test_real_break_survives_recheck():
    fills = [make_fill("yes", 200, 0.10, ticker=TK)]
    s = snap(fills, [position(TK, 170, 0.0)], deposits=funded(2_000))
    r = rc.recheck(FreshClient([position(TK, 170, 0.0)], fills), s, reconcile(s))
    assert kinds(r) == ["position_mismatch"] and r.rechecked_clean == 0


def test_breaks_are_sorted_largest_first():
    fills = [make_fill("yes", 100, 0.20, fee=0.50, ticker=TK)]
    r = reconcile(snap(fills, [position(TK, 40, 0.0, fees=0.75)]))
    mags = [b.magnitude for b in r.breaks]
    assert mags == sorted(mags, reverse=True)
