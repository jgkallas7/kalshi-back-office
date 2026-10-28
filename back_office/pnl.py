"""Average-cost P&L engine for Kalshi binary event contracts.

Pure functions: fills in, numbers out. No I/O, no database, no network, so the
same code runs against the live API, the stored audit log, and test fixtures.

Kalshi mechanics that make this non-trivial
-------------------------------------------
* Every fill carries both `yes_price_dollars` and `no_price_dollars`; they sum to $1.
* A position is held in YES-equivalent contracts: holding NO is being short YES.
* Because no_price = 1 - yes_price, every fill's execution price in YES terms is
  simply its yes_price. The fill's side only decides the SIGN of the position change:

      side == "yes"  ->  +qty        side == "no"  ->  -qty        price = yes_price

  The order's buy/sell action is not needed: Kalshi nets YES and NO holdings in
  the same market, so closing a long YES shows up as a fill on the NO side. This
  convention was validated market by market against Kalshi's own reported
  position and realized P&L on a live account.

Lot accounting
--------------
Average cost. Fills that open or add to a position re-weight the average entry
price; fills that reduce it realize (exit - avg) per contract closed, signed by
direction; a fill that crosses zero closes the old lot and opens a new one at
its own price. Fees are tracked separately per fill so gross and net can both be
reported. When a market settles, any residual position is closed at the payout
($1.00 if YES won, $0.00 if NO won).
"""
from __future__ import annotations

from dataclasses import dataclass

from .fields import fill_qty, fill_side, num


def _is_api_shape(fill: dict) -> bool:
    """API fills carry *_dollars fields; stored rows carry integer/decimal cents.

    The unit is detected from the FIELD NAME, never from the magnitude of the
    value. A rule like "price > 1.5 means cents" silently reads a 1-cent fill
    stored as `1` as $1.00 -- a 100x error on exactly the cheap contracts that
    trade the most.
    """
    return fill.get("yes_price_dollars") is not None


def yes_delta_and_price(fill: dict) -> tuple[float, float]:
    """(signed YES-equivalent quantity change, YES execution price in dollars)."""
    qty = fill_qty(fill)
    if _is_api_shape(fill):
        price = num(fill["yes_price_dollars"])
    else:
        price = num(fill.get("yes_price")) / 100.0
    return (qty if fill_side(fill) == "yes" else -qty), price


def fee_dollars(fill: dict) -> float:
    """`fee_cost` is dollars on API fills and cents on stored rows."""
    fee = num(fill.get("fee_cost"))
    return fee if _is_api_shape(fill) else fee / 100.0


@dataclass
class MarketPnL:
    ticker: str
    realized: float = 0.0        # closed-lot P&L from trading, before fees
    fees: float = 0.0            # all fill fees paid in this market
    position: float = 0.0        # net YES-equivalent contracts (negative = short YES / long NO)
    avg_cost: float = 0.0        # average YES-equivalent entry price of the open lot
    settled_pnl: float = 0.0     # P&L booked when the market settled on the residual position
    settled: bool = False
    n_fills: int = 0

    @property
    def realized_net(self) -> float:
        """Trading P&L + settlement P&L, net of all fees: the market's closed P&L."""
        return self.realized + self.settled_pnl - self.fees

    @property
    def realized_gross_incl_settlement(self) -> float:
        return self.realized + self.settled_pnl

    def unrealized(self, mark_yes_price: float) -> float:
        """Mark-to-market of the open position at a YES price in dollars."""
        return self.position * (mark_yes_price - self.avg_cost)

    @property
    def open_cost_basis(self) -> float:
        """Cash tied up in the open position.

        Long YES costs avg * qty. Short YES is really long NO, which cost
        (1 - avg) per contract. Used for the cash identity check.
        """
        if self.position > 0:
            return self.avg_cost * self.position
        if self.position < 0:
            return (1.0 - self.avg_cost) * -self.position
        return 0.0


def _sort_key(fill: dict):
    return fill.get("created_time") or fill.get("ts") or ""


def apply_fill(m: MarketPnL, fill: dict) -> None:
    """Advance the average-cost walk by one fill."""
    m.n_fills += 1
    m.fees += fee_dollars(fill)
    d, px = yes_delta_and_price(fill)
    if d == 0:
        return
    pos, avg = m.position, m.avg_cost
    if pos == 0 or (pos > 0) == (d > 0):
        # opening or adding in the same direction: quantity-weighted average
        new = pos + d
        m.avg_cost = (avg * pos + px * d) / new if new != 0 else 0.0
        m.position = new
        return
    # reducing, closing, or flipping through zero
    closing = min(abs(d), abs(pos))
    # a long closed at px earns (px - avg); a short closed at px earns (avg - px)
    m.realized += (px - avg) * closing * (1 if pos > 0 else -1)
    new = pos + d
    if new != 0 and (new > 0) != (pos > 0):
        m.avg_cost = px          # flipped: the residual is a fresh lot at this price
    m.position = new
    if new == 0:
        m.avg_cost = 0.0


def settle(m: MarketPnL, settle_yes_value: float) -> None:
    """Close any residual position at the settlement payout."""
    if abs(m.position) > 1e-9:
        m.settled_pnl = m.position * (settle_yes_value - m.avg_cost)
    m.position = 0.0
    m.avg_cost = 0.0
    m.settled = True


def compute_market(ticker: str, fills: list[dict],
                   settle_yes_value: float | None = None) -> MarketPnL:
    """Average-cost accounting for one market.

    Args:
        fills: Kalshi fill records for this ticker, in any order.
        settle_yes_value: the YES payout if the market has settled (1.0 or 0.0
            for binary markets), or None if it is still open.
    """
    m = MarketPnL(ticker=ticker)
    for fill in sorted(fills, key=_sort_key):
        apply_fill(m, fill)
    if settle_yes_value is not None:
        settle(m, settle_yes_value)
    return m
