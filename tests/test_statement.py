import sqlite3

from back_office.reconcile import Snapshot, init_db
from back_office.statement import build, render_text


def _snap(as_of, balance_cents):
    return Snapshot(as_of=as_of, balance_cents=balance_cents, portfolio_value_cents=0,
                    deposits=[{"amount_cents": 100_000, "status": "applied"}], withdrawals=[],
                    positions=[], fills=[], settlements=[])


def _stored(conn, run_ts, equity):
    init_db(conn)
    conn.execute("INSERT INTO recon_runs (run_ts, equity, net_deposits, cash_identity_residual) "
                 "VALUES (?,?,?,?)", (run_ts, equity, 1000.0, 0.0))


def test_daily_pnl_is_labelled_as_a_day():
    conn = sqlite3.connect(":memory:")
    _stored(conn, 1_000_000 - 86_400, 1000.0)
    st = build(_snap(1_000_000, 100_500), conn)
    assert "Daily P&L (vs ~24h ago): $+5.00" in render_text(st)


def test_missed_run_says_how_long_the_window_really_is():
    conn = sqlite3.connect(":memory:")
    _stored(conn, 1_000_000 - 2 * 86_400, 1000.0)       # yesterday's run never happened
    st = build(_snap(1_000_000, 100_500), conn)
    assert "Daily P&L (vs 48h ago: a daily run was missed): $+5.00" in render_text(st)
