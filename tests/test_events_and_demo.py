import sqlite3

from conftest import make_fill

from back_office import events
from back_office.demo import FixtureClient, run_demo, snapshot_from
from back_office.reconcile import reconcile


def test_capture_is_idempotent_and_closes_orders():
    conn = sqlite3.connect(":memory:")
    fills = [make_fill("yes", 10, 0.5, fill_id="f1")]
    fills[0]["order_id"] = "o1"
    resting = [{"order_id": "o1", "ticker": "KXEXAMPLE-26JAN01-A", "side": "yes"},
               {"order_id": "o2", "ticker": "KXEXAMPLE-26JAN01-A", "side": "no"}]
    first = events.capture(conn, [], [], resting, now=1)
    assert first == {"fill": 0, "settlement": 0, "placed": 2, "closed": 0}
    # o1 filled and left the book; o2 was pulled
    second = events.capture(conn, fills, [], [], now=2)
    assert second == {"fill": 1, "settlement": 0, "placed": 0, "closed": 2}
    closed = dict(conn.execute("SELECT order_id, detail FROM order_events WHERE event_type='closed'"))
    assert closed == {"o1": "filled", "o2": "canceled"}
    # re-running adds nothing
    assert sum(events.capture(conn, fills, [], [], now=3).values()) == 0


def test_demo_account_flags_exactly_the_planted_problems():
    r = reconcile(snapshot_from(FixtureClient.bundled()))
    found = {(b.kind, b.ticker) for b in r.breaks}
    assert found == {("position_mismatch", "KXEXAMPLE-26JAN14-F"),
                     ("fee_mismatch", "KXEXAMPLE-26JAN10-A")}
    assert round(r.cash_identity_residual, 2) == 2.14   # the interest, and nothing else
    assert sum(m.settled for m in r.markets.values()) == 4
    # the old market is closed from the archive, not left open forever
    old = r.markets["KXEXAMPLE-25NOV08-G"]
    assert old.settled and old.position == 0 and round(old.settled_pnl, 2) == 97.50


def test_demo_runs_end_to_end(tmp_path, capsys):
    path = run_demo(str(tmp_path))
    out = capsys.readouterr().out
    assert "2 RECONCILIATION BREAK(S)" in out
    assert "Non-trading cash (interest, rewards): $+2.14  (+0.07 since the last run)" in out
    assert "Daily P&L (vs ~24h ago): $+41.37" in out
    html = open(path).read()
    assert "Kalshi Account Statement" in html and "KXEXAMPLE-26JAN14-F" in html
