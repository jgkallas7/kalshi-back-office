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
cash_identity       cash and engine disagree in a way non-trading cash can't explain
cash_drift          the cash-identity residual jumped since the previous run

The cash identity never ties to zero on a real account: Kalshi pays interest
on balances and runs reward programs, and neither shows up in fills or
settlements. That difference is the residual. It should be small, positive and
slow-moving, so the checks are: never negative (cash missing), never more than
interest could plausibly add up to, and no jumps between runs.

The data gathering (`fetch_snapshot`, `recheck`) is separate from the
comparison (`reconcile`), so the comparison is a pure function that tests and
the demo can drive with fixture data.
"""
from __future__ import annotations

import sqlite3
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import datetime

from .fields import fill_side, historical_settle_value, num, settle_yes_value
from .pnl import MarketPnL, compute_market

POS_TOL = 0.01      # contracts
PNL_TOL = 0.05      # dollars, per market
FEE_TOL = 0.05      # dollars, per market
CASH_TOL = 10.00    # dollars: how far cash may fall short of the engine (fee rounding)

# Non-trading cash (interest, rewards). Interest posts monthly, so between two
# daily runs the residual can move by up to a month of interest. The tolerance
# is one month at a generous ceiling rate, with a floor for small accounts.
INTEREST_APY_CEIL = 0.06
CASH_DRIFT_FLOOR = 25.00

PER_MARKET_KINDS = ("position_mismatch", "realized_mismatch", "fee_mismatch")
RECHECK_MAX = 40    # more flagged markets than this is systemic, not a timing race

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
    # Markets that settled before the live settlements endpoint's retention
    # window, from /historical/markets. Without them every older market held to
    # expiry looks open forever.
    historical_markets: list[dict] = field(default_factory=list)


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
    prev_residual: float | None = None   # residual on the previous stored run
    markets_venue_checked: int = 0       # markets the exchange still reports
    rechecked_clean: int = 0             # breaks that cleared on a fresh fetch

    @property
    def markets_reconciled(self) -> int:
        return len(self.markets)


def fetch_snapshot(client) -> Snapshot:
    """Pull a consistent view of the account from the API (read-only)."""
    import time
    balance, portfolio_value = client.get_balance()
    fills = client.get_fills()
    settlements = client.get_settlements()
    have = {s["ticker"] for s in settlements}
    older = sorted({f["ticker"] for f in fills} - have)
    return Snapshot(
        as_of=int(time.time()),
        balance_cents=balance,
        portfolio_value_cents=portfolio_value,
        deposits=client.get_deposits(),
        withdrawals=client.get_withdrawals(),
        positions=client.get_positions(),
        fills=fills,
        settlements=settlements,
        historical_markets=client.get_historical_markets(older) if older else [],
    )


def _applied(records: list[dict]) -> list[dict]:
    return [r for r in records if r.get("status") == "applied"]


def _epoch(ts) -> float | None:
    """Funding records carry created_ts as epoch seconds (API) or ISO text (stored)."""
    if ts in (None, ""):
        return None
    try:
        return float(ts)
    except (TypeError, ValueError):
        return datetime.fromisoformat(str(ts).replace("Z", "+00:00")).timestamp()


def settlement_values(settlements: list[dict], historical_markets: list[dict]) -> dict[str, float]:
    """YES payout per market from both sources. The live record wins when both exist."""
    out: dict[str, float] = {}
    for st in settlements:
        v = settle_yes_value(st)
        if v is not None:
            out[st["ticker"]] = v
    for mk in historical_markets:
        tk = mk.get("ticker")
        v = historical_settle_value(mk)
        if tk and tk not in out and v is not None:
            out[tk] = v
    return out


def market_breaks(tk: str, m: MarketPnL | None, p: dict) -> list[Break]:
    """Engine vs exchange for one market the exchange reports."""
    k_pos = num(p.get("position_fp") if p.get("position_fp") is not None else p.get("position"))
    if m is None:
        if abs(k_pos) > POS_TOL:
            return [Break("missing_fills", tk,
                          f"Kalshi position {k_pos:,.2f} but no fills on record", abs(k_pos))]
        return []
    out = []
    if abs(m.position - k_pos) > POS_TOL:
        out.append(Break("position_mismatch", tk,
                         f"engine {m.position:,.2f} vs Kalshi {k_pos:,.2f}",
                         abs(m.position - k_pos)))
    k_real = num(p.get("realized_pnl_dollars"))
    eng_real = (m.realized_gross_incl_settlement
                if VENUE_REALIZED_INCLUDES_SETTLEMENT else m.realized)
    if abs(eng_real - k_real) > PNL_TOL:
        out.append(Break("realized_mismatch", tk,
                         f"engine ${eng_real:,.2f} vs Kalshi ${k_real:,.2f}",
                         abs(eng_real - k_real)))
    if p.get("fees_paid_dollars") is not None:
        k_fees = num(p.get("fees_paid_dollars"))
        if abs(m.fees - k_fees) > FEE_TOL:
            out.append(Break("fee_mismatch", tk,
                             f"engine ${m.fees:,.2f} vs Kalshi ${k_fees:,.2f}",
                             abs(m.fees - k_fees)))
    return out


def drift_tolerance(equity: float) -> float:
    """How far the residual may move between runs: one month of interest at the ceiling rate."""
    return max(CASH_DRIFT_FLOOR, max(equity, 0.0) * INTEREST_APY_CEIL / 12)


def non_trading_ceiling(deposits: list[dict], withdrawals: list[dict], equity: float,
                        as_of: int) -> float:
    """The most non-trading cash the account could plausibly have earned.

    Interest at the ceiling rate, for the life of the account, on the most money
    that has ever been in it (bounded by gross deposits, or by everything taken
    out plus what is still there). Generous on purpose: it exists to catch an
    engine that is off by thousands, not to audit the interest rate.
    """
    starts = [t for t in (_epoch(d.get("created_ts")) for d in deposits) if t is not None]
    if not starts:
        return CASH_TOL
    years = max(as_of - min(starts), 0) / (365 * 86_400)
    gross_in = sum(num(d.get("amount_cents")) for d in deposits) / 100.0
    gross_out = sum(num(w.get("amount_cents")) for w in withdrawals) / 100.0
    return max(CASH_TOL, INTEREST_APY_CEIL * years * max(gross_in, gross_out + max(equity, 0.0)))


def reconcile(s: Snapshot, prev_residual: float | None = None) -> ReconResult:
    breaks: list[Break] = []

    # ---- 1. cash view ---------------------------------------------------------
    cash = s.balance_cents / 100.0
    portfolio_value = s.portfolio_value_cents / 100.0
    equity = cash + portfolio_value
    deps, wds = _applied(s.deposits), _applied(s.withdrawals)
    net_deposits = (sum(num(d.get("amount_cents")) for d in deps)
                    - sum(num(w.get("amount_cents")) for w in wds)) / 100.0
    cash_pnl = equity - net_deposits

    # ---- 2. engine view -------------------------------------------------------
    fills_by_ticker: dict[str, list[dict]] = defaultdict(list)
    for f in s.fills:
        if not fill_side(f):
            breaks.append(Break("unknown_side", f.get("ticker", "?"),
                                f"fill {f.get('fill_id', '?')} has no side/outcome_side", 0.0))
            continue
        fills_by_ticker[f["ticker"]].append(f)

    # Settlement is applied to the engine position. Without it, every position
    # held to expiry stays "open" in the engine forever while the exchange shows
    # it closed. Older settlements come from the historical markets archive.
    settle_value = settlement_values(s.settlements, s.historical_markets)
    markets = {tk: compute_market(tk, fl, settle_value.get(tk))
               for tk, fl in fills_by_ticker.items()}

    # ---- 3. engine vs venue, market by market ---------------------------------
    # Kalshi only reports positions for markets inside its retention window, so
    # this check covers recent markets; the cash identity below covers them all.
    for p in s.positions:
        breaks.extend(market_breaks(p["ticker"], markets.get(p["ticker"]), p))

    # ---- 4. account-level cash identity ---------------------------------------
    # lifetime cash P&L = closed P&L (net of fees) + (mark value - cost) of what
    # is still open + non-trading cash (interest, rewards). The residual is the
    # non-trading cash, and it is checked three ways.
    engine_realized_net = sum(m.realized_net for m in markets.values())
    engine_fees = sum(m.fees for m in markets.values())
    open_cost = sum(m.open_cost_basis for m in markets.values())
    engine_open_pnl = portfolio_value - open_cost
    engine_total = engine_realized_net + engine_open_pnl
    residual = cash_pnl - engine_total
    ceiling = non_trading_ceiling(deps, wds, equity, s.as_of)
    if residual < -CASH_TOL:
        breaks.append(Break("cash_identity", "ACCOUNT",
                            f"cash P&L ${cash_pnl:,.2f} is ${-residual:,.2f} short of the engine "
                            f"${engine_total:,.2f}: a loss or outflow the fills don't show",
                            -residual))
    elif residual > ceiling:
        breaks.append(Break("cash_identity", "ACCOUNT",
                            f"cash P&L ${cash_pnl:,.2f} is ${residual:,.2f} above the engine "
                            f"${engine_total:,.2f}, more than interest could explain "
                            f"(ceiling ${ceiling:,.2f}): missing settlements or fills",
                            residual))
    tol = drift_tolerance(equity)
    if prev_residual is not None and abs(residual - prev_residual) > tol:
        breaks.append(Break("cash_drift", "ACCOUNT",
                            f"non-trading cash moved ${prev_residual:,.2f} -> ${residual:,.2f} "
                            f"since the last run (tolerance ${tol:,.2f})",
                            abs(residual - prev_residual)))

    open_pos = [p for p in s.positions if abs(num(p.get("position_fp"))) > POS_TOL]
    return ReconResult(
        as_of=s.as_of, equity=equity, cash=cash, portfolio_value=portfolio_value,
        net_deposits=net_deposits, n_deposits=len(deps), n_withdrawals=len(wds),
        cash_pnl=cash_pnl, engine_realized_net=engine_realized_net, engine_fees=engine_fees,
        engine_open_pnl=engine_open_pnl, cash_identity_residual=residual, markets=markets,
        open_positions=len(open_pos),
        open_exposure=sum(num(p.get("market_exposure_dollars")) for p in open_pos),
        breaks=sorted(breaks, key=lambda b: -b.magnitude),
        prev_residual=prev_residual, markets_venue_checked=len(s.positions),
    )


def recheck(client, s: Snapshot, r: ReconResult) -> ReconResult:
    """Re-fetch each flagged market on its own before reporting it.

    The snapshot is a handful of API calls spread over a few seconds. A fill
    that lands between the positions call and the fills call makes the two
    disagree for that one run. A break that disappears on a fresh single-market
    fetch was that race, not a problem in the book, and is dropped.
    """
    flagged = sorted({b.ticker for b in r.breaks if b.kind in PER_MARKET_KINDS})
    if not flagged or len(flagged) > RECHECK_MAX:
        return r
    settle_value = settlement_values(s.settlements, s.historical_markets)
    keep = [b for b in r.breaks if b.ticker not in flagged or b.kind not in PER_MARKET_KINDS]
    cleared = 0
    for tk in flagged:
        venue = next((p for p in client.get_positions(ticker=tk) if p.get("ticker") == tk), None)
        fills = {f["fill_id"]: f for f in s.fills if f.get("ticker") == tk}
        for f in client.get_fills(ticker=tk, include_historical=False):
            fills.setdefault(f["fill_id"], f)
        fresh = settlement_values(client.get_settlements(ticker=tk), [])
        m = compute_market(tk, list(fills.values()), fresh.get(tk, settle_value.get(tk)))
        bks = market_breaks(tk, m, venue) if venue is not None else []
        if bks:
            keep.extend(bks)
        else:
            cleared += 1
    r.breaks = sorted(keep, key=lambda b: -b.magnitude)
    r.rechecked_clean = cleared
    return r


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


def previous_residual(conn: sqlite3.Connection, before: int) -> float | None:
    """The cash-identity residual on the last stored run before this one."""
    init_db(conn)
    row = conn.execute(
        "SELECT cash_identity_residual FROM recon_runs WHERE run_ts < ? "
        "AND cash_identity_residual IS NOT NULL ORDER BY run_ts DESC LIMIT 1", (before,)).fetchone()
    return None if row is None else row[0]


def residual_note(r: ReconResult) -> str:
    if r.prev_residual is None:
        return "first tracked run: review this baseline once"
    return f"{r.cash_identity_residual - r.prev_residual:+,.2f} since the last run"


def render_report(r: ReconResult) -> str:
    open_unsettled = sum(1 for m in r.markets.values() if abs(m.position) > POS_TOL)
    lines = [
        "CASH VIEW",
        f"  equity (cash + positions)  ${r.equity:>12,.2f}",
        f"  net deposits               ${r.net_deposits:>12,.2f}   "
        f"({r.n_deposits} deposits, {r.n_withdrawals} withdrawals)",
        f"  lifetime cash P&L          ${r.cash_pnl:>12,.2f}",
        "",
        "ENGINE VIEW",
        f"  markets rebuilt from fills {r.markets_reconciled:>13}   "
        f"({open_unsettled} still open)",
        f"  markets checked vs Kalshi  {r.markets_venue_checked:>13}   "
        f"({r.rechecked_clean} cleared on re-fetch)",
        f"  realized P&L, net of fees  ${r.engine_realized_net:>12,.2f}",
        f"  open P&L at mark           ${(0.0 if abs(r.engine_open_pnl) < 0.005 else r.engine_open_pnl):>12,.2f}",
        f"  fees paid                  ${r.engine_fees:>12,.2f}",
        f"  non-trading cash           ${(0.0 if abs(r.cash_identity_residual) < 0.005 else r.cash_identity_residual):>12,.2f}"
        f"   (interest, rewards; {residual_note(r)})",
        "",
        f"BREAKS: {len(r.breaks)}",
    ]
    lines += [f"  [{b.kind}] {b.ticker}: {b.detail}" for b in r.breaks[:25]]
    if not r.breaks:
        lines.append("  none: engine ties to the exchange and to cash")
    return "\n".join(lines)
