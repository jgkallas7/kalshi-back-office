"""Parsing helpers for Kalshi API records.

Kalshi's V3 API returns prices and quantities as decimal strings
(`"0.4500"`, `"2967.88"`), money in a mix of dollar strings and integer cents,
and is migrating fill direction from legacy fields (`side`, `action`) to
`outcome_side`. Everything that reads those records goes through here, so a
field rename only has to be handled in one place.
"""
from __future__ import annotations


def num(x) -> float:
    """Parse a Kalshi numeric field (string, int, float, or missing) to float."""
    try:
        return float(x)
    except (TypeError, ValueError):
        return 0.0


def fill_side(fill: dict) -> str:
    """'yes' or 'no': which contract the fill transacted.

    Prefers the legacy `side`, falls back to `outcome_side` (they carry the same
    value on fills). Returns '' when neither is present so callers can flag it
    instead of guessing.
    """
    for key in ("side", "outcome_side"):
        v = str(fill.get(key) or "").lower()
        if v in ("yes", "no"):
            return v
    return ""


def fill_qty(fill: dict) -> float:
    """Contract count. `count_fp` is the V3 field; `count` is the stored/legacy one."""
    return num(fill.get("count_fp") if fill.get("count_fp") is not None else fill.get("count"))


def settle_yes_value(settlement: dict) -> float | None:
    """YES payout per contract in dollars (1.0 = YES won, 0.0 = NO won).

    Uses `market_result` when it is a plain yes/no; otherwise falls back to
    `value` (integer cents per YES contract, used for scalar outcomes).
    Returns None if the record carries neither.
    """
    result = str(settlement.get("market_result") or "").lower()
    if result == "yes":
        return 1.0
    if result == "no":
        return 0.0
    if settlement.get("value") is not None:
        return num(settlement["value"]) / 100.0
    return None


SETTLED_STATUSES = ("finalized", "settled")


def historical_settle_value(market: dict) -> float | None:
    """YES payout per contract from a /historical/markets record.

    Markets that settled before the live settlements endpoint's retention window
    only appear there, with `settlement_value_dollars`. A market that is not
    final yet, or carries no value, returns None (still open), never 0.0: a
    silent zero would book every YES winner as a loss.
    """
    if market.get("status") not in SETTLED_STATUSES:
        return None
    v = market.get("settlement_value_dollars")
    if v in (None, ""):
        return None
    return num(v)
