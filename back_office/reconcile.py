"""Daily reconciliation: the engine's book against the exchange's book.

Three independent views of the same account are compared, and every
disagreement becomes a BREAK for a human to look at:

1. Cash view    equity (cash + positions at mark) minus net deposits
                = lifetime P&L, straight from the balance and funding endpoints.
2. Engine view  average-cost P&L rebuilt from raw fills and settlements
                (back_office.pnl), market by market.
3. Venue view   Kalshi's own per-market position, realized P&L and fees.

Break types
-----------
position_mismatch   engine position != Kalshi position for a market
realized_mismatch   engine realized P&L != Kalshi realized P&L
fee_mismatch        engine fees != Kalshi fees paid
missing_fills       Kalshi shows an open position but there are no fills for it
unknown_side        a fill has no readable side, so it cannot be signed
cash_identity       lifetime cash P&L != engine realized (net) + open P&L at mark

The data gathering (`fetch_snapshot`) is separate from the comparison
(`reconcile`), so the comparison is a pure function that tests and the demo can
drive with fixture data.
"""
from __future__ import annotations

import sqlite3
from collections import defaultdict
from dataclasses import dataclass, field

from .fields import fill_side, num, settle_yes_value
from .pnl import MarketPnL, compute_market

POS_TOL = 0.01      # contracts
PNL_TOL = 0.05      # dollars, per market
FEE_TOL = 0.05      # dollars, per market
CASH_TOL = 10.00    # dollars, whole account (sub-cent fee rounding accumulates)

# Kalshi's per-market realized_pnl_dollars: does it include the payout on a
# position held to settlement? Set to match the venue's definition; the
# realized_mismatch check compares like with like either way.
VENUE_REALIZED_INCLUDES_SETTLEMENT = True


@dataclass
class Snapshot:
    """Everything the reconciliation needs, captured at one point in time."""
    as_of: int                     # unix seconds
    balance_cents: int
    portfolio_value_cents: int
    deposits: list[dict]
    withdrawals: list[dict]
    positions: list[dict]
    fills: list[dict]
    settlements: list[dict]


@dataclass
class Break:
    kind: str
    ticker: str
    detail: str
    magnitude: float


@dataclass
class ReconResult:
    as_of: int
    equity: float
    cash: float
    portfolio_value: float
    net_deposits: float
    n_deposits: int
    n_withdrawals: int
    cash_pnl: float
    engine_realized_net: float
    engine_fees: float
    engine_open_pnl: float
    cash_identity_residual: float
    markets: dict[str, MarketPnL]
    open_positions: int
    open_exposure: float
    breaks: list[Break] = field(default_factory=list)

    @property
    def markets_reconciled(self) -> int:
        return len(self.markets)


def fetch_snapshot(client) -> Snapshot:
    """Pull a consistent view of the account from the API (read-only)."""
    import time
    balance, portfolio_value = client.get_balance()
    return Snapshot(
        as_of=int(time.time()),
        balance_cents=balance,
        portfolio_value_cents=portfolio_value,
        deposits=client.get_deposits(),
        withdrawals=client.get_withdrawals(),
        positions=client.get_positions(),
        fills=client.get_fills(),
        settlements=client.get_settlements(),
    )


def _applied_total(records: list[dict]) -> tuple[float, int]:
    applied = [r for r in records if r.get("status") == "applied"]
    return sum(num(r.get("amount_cents")) for r in applied) / 100.0, len(applied)


def reconcile(s: Snapshot) -> ReconResult:
    breaks: list[Break] = []

    # ---- 1. cash view ---------------------------------------------------------
    cash = s.balance_cents / 100.0
    portfolio_value = s.portfolio_value_cents / 100.0
    equity = cash + portfolio_value
    dep, n_dep = _applied_total(s.deposits)
    wd, n_wd = _applied_total(s.withdrawals)
    net_deposits = dep - wd
    cash_pnl = equity - net_deposits

    # ---- 2. engine view -------------------------------------------------------
    fills_by_ticker: dict[str, list[dict]] = defaultdict(list)
    for f in s.fills:
        if not fill_side(f):
            breaks.append(Break("unknown_side", f.get("ticker", "?"),
                                f"fill {f.get('fill_id', '?')} has no side/outcome_side", 0.0))
            continue
        fills_by_ticker[f["ticker"]].append(f)

    settle_value: dict[str, float] = {}
    for st in s.settlements:
        v = settle_yes_value(st)
        if v is not None:
            settle_value[st["ticker"]] = v

    # Settlement is applied to the engine position. Without it, every position
    # held to expiry stays "open" in the engine forever while the exchange shows
    # it closed -- a position_mismatch on every settled market.
    markets = {tk: compute_market(tk, fl, settle_value.get(tk))
               for tk, fl in fills_by_ticker.items()}

    # ---- 3. engine vs venue, market by market ---------------------------------
    for p in s.positions:
        tk = p["ticker"]
        k_pos = num(p.get("position_fp") if p.get("position_fp") is not None else p.get("position"))
        k_real = num(p.get("realized_pnl_dollars"))
        k_fees = num(p.get("fees_paid_dollars"))
        m = markets.get(tk)
        if m is None:
            if abs(k_pos) > POS_TOL:
                breaks.append(Break("missing_fills", tk,
                                    f"Kalshi position {k_pos:,.2f} but no fills on record", abs(k_pos)))
            continue
        if abs(m.position - k_pos) > POS_TOL:
            breaks.append(Break("position_mismatch", tk,
                                f"engine {m.position:,.2f} vs Kalshi {k_pos:,.2f}",
                                abs(m.position - k_pos)))
        eng_real = (m.realized_gross_incl_settlement
                    if VENUE_REALIZED_INCLUDES_SETTLEMENT else m.realized)
        if abs(eng_real - k_real) > PNL_TOL:
            breaks.append(Break("realized_mismatch", tk,
                                f"engine ${eng_real:,.2f} vs Kalshi ${k_real:,.2f}",
                                abs(eng_real - k_real)))
        if p.get("fees_paid_dollars") is not None and abs(m.fees - k_fees) > FEE_TOL:
            breaks.append(Break("fee_mismatch", tk,
                                f"engine ${m.fees:,.2f} vs Kalshi ${k_fees:,.2f}",
                                abs(m.fees - k_fees)))

    # ---- 4. account-level cash identity ---------------------------------------
    # lifetime cash P&L = closed P&L (net of fees) + (mark value - cost) of what is still open
    engine_realized_net = sum(m.realized_net for m in markets.values())
    engine_fees = sum(m.fees for m in markets.values())
    open_cost = sum(m.open_cost_basis for m in markets.values())
    engine_open_pnl = portfolio_value - open_cost
    residual = cash_pnl - (engine_realized_net + engine_open_pnl)
    if abs(residual) > CASH_TOL:
        breaks.append(Break("cash_identity", "ACCOUNT",
                            f"cash P&L ${cash_pnl:,.2f} vs engine ${engine_realized_net + engine_open_pnl:,.2f}",
                            abs(residual)))

    open_pos = [p for p in s.positions if abs(num(p.get("position_fp"))) > POS_TOL]
    return ReconResult(
        as_of=s.as_of, equity=equity, cash=cash, portfolio_value=portfolio_value,
        net_deposits=net_deposits, n_deposits=n_dep, n_withdrawals=n_wd, cash_pnl=cash_pnl,
        engine_realized_net=engine_realized_net, engine_fees=engine_fees,
        engine_open_pnl=engine_open_pnl, cash_identity_residual=residual, markets=markets,
        open_positions=len(open_pos),
        open_exposure=sum(num(p.get("market_exposure_dollars")) for p in open_pos),
        breaks=sorted(breaks, key=lambda b: -b.magnitude),
    )


# ---- persistence (audit trail of every run) ------------------------------------

def init_db(conn: sqlite3.Connection) -> None:
    conn.execute("""CREATE TABLE IF NOT EXISTS recon_runs (
        run_ts INTEGER PRIMARY KEY, equity REAL, net_deposits REAL,
        cash_pnl REAL, engine_realized_net REAL, cash_identity_residual REAL, n_breaks INTEGER)""")
    conn.execute("""CREATE TABLE IF NOT EXISTS recon_breaks (
        run_ts INTEGER, kind TEXT, ticker TEXT, detail TEXT, magnitude REAL)""")
    conn.commit()


def persist(conn: sqlite3.Connection, r: ReconResult) -> None:
    init_db(conn)
    conn.execute("INSERT OR REPLACE INTO recon_runs VALUES (?,?,?,?,?,?,?)",
                 (r.as_of, r.equity, r.net_deposits, r.cash_pnl, r.engine_realized_net,
                  r.cash_identity_residual, len(r.breaks)))
    conn.execute("DELETE FROM recon_breaks WHERE run_ts = ?", (r.as_of,))
    conn.executemany("INSERT INTO recon_breaks VALUES (?,?,?,?,?)",
                     [(r.as_of, b.kind, b.ticker, b.detail, b.magnitude) for b in r.breaks])
    conn.commit()


def baseline_run(conn: sqlite3.Connection, at_or_before: int) -> dict | None:
    """The most recent stored run at or before a timestamp (yesterday's statement)."""
    init_db(conn)
    row = conn.execute(
        "SELECT run_ts, equity, net_deposits FROM recon_runs WHERE run_ts <= ? "
        "ORDER BY run_ts DESC LIMIT 1", (at_or_before,)).fetchone()
    return None if row is None else {"run_ts": row[0], "equity": row[1], "net_deposits": row[2]}


def render_report(r: ReconResult) -> str:
    lines = [
        "CASH VIEW",
        f"  equity (cash + positions)  ${r.equity:>12,.2f}",
        f"  net deposits               ${r.net_deposits:>12,.2f}   "
        f"({r.n_deposits} deposits, {r.n_withdrawals} withdrawals)",
        f"  lifetime cash P&L          ${r.cash_pnl:>12,.2f}",
        "",
        "ENGINE VIEW",
        f"  markets rebuilt from fills {r.markets_reconciled:>13}",
        f"  realized P&L, net of fees  ${r.engine_realized_net:>12,.2f}",
        f"  open P&L at mark           ${r.engine_open_pnl:>12,.2f}",
        f"  fees paid                  ${r.engine_fees:>12,.2f}",
        f"  cash identity residual     ${(0.0 if abs(r.cash_identity_residual) < 0.005 else r.cash_identity_residual):>12,.2f}",
        "",
        f"BREAKS: {len(r.breaks)}",
    ]
    lines += [f"  [{b.kind}] {b.ticker}: {b.detail}" for b in r.breaks[:25]]
    if not r.breaks:
        lines.append("  none: engine ties to the exchange and to cash")
    return "\n".join(lines)
