"""Generate the synthetic sample account used by `python -m back_office demo`.

Everything here is made up: tickers, prices, sizes and balances. The fixture is
built so the account ties out, except for two deliberately planted problems the
reconciliation is expected to catch. It also has the two things a real account
has that a naive reconciler gets wrong:

  * KXEXAMPLE-25NOV08-G  settled long ago: its fills are in the historical
                         archive, its settlement only in /historical/markets,
                         and the exchange no longer reports a position for it
  * $2.14 of interest    cash the fills and settlements never mention

The planted problems:

  * KXEXAMPLE-26JAN14-F  the exchange reports 30 fewer contracts than the fills
                         add up to (a fill missing from the record)
  * KXEXAMPLE-26JAN10-A  the exchange reports $0.25 more in fees than the fills

Run from the repo root:  python tools/make_demo_fixture.py
"""
from __future__ import annotations

import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from back_office.pnl import compute_market  # noqa: E402

AS_OF = "2026-01-15T22:00:00Z"
AS_OF_TS = 1768514400

_n = 0


def fill(ticker, side, action, qty, yes_price, fee, t, taker=False, order_id=None):
    global _n
    _n += 1
    return {
        "fill_id": f"demo-fill-{_n:03d}", "order_id": order_id or f"demo-order-{_n:03d}",
        "ticker": ticker, "side": side, "outcome_side": side, "action": action,
        "count_fp": f"{qty:.2f}", "yes_price_dollars": f"{yes_price:.4f}",
        "no_price_dollars": f"{1 - yes_price:.4f}", "fee_cost": f"{fee:.4f}",
        "is_taker": taker, "created_time": t,
    }


A, B, C, D, E, F, G = (f"KXEXAMPLE-{s}" for s in
                       ("26JAN10-A", "26JAN10-B", "26JAN12-C", "26JAN18-D", "26JAN20-E",
                        "26JAN14-F", "25NOV08-G"))
INTEREST = 2.14            # credited to cash; appears in no fill or settlement
INTEREST_YESTERDAY = 2.07

historical = [   # older than the exchange's live retention window
    fill(G, "yes", "buy", 150, 0.35, 0.80, "2025-11-04T15:00:00Z"),
    fill(A, "yes", "buy", 400, 0.22, 1.50, "2025-11-03T15:00:00Z"),
    fill(A, "no", "sell", 400, 0.31, 1.20, "2025-11-20T18:30:00Z", taker=True),
]
live = [
    fill(B, "yes", "buy", 1000, 0.04, 0.70, "2026-01-08T14:00:00Z"),
    fill(C, "no", "buy", 300, 0.65, 2.10, "2026-01-11T16:00:00Z"),
    fill(F, "yes", "buy", 200, 0.10, 0.65, "2026-01-12T13:00:00Z"),
    fill(F, "no", "sell", 120, 0.15, 0.55, "2026-01-13T20:00:00Z", taker=True),
    fill(D, "yes", "buy", 500, 0.40, 1.75, "2026-01-15T14:10:00Z"),
    fill(D, "no", "sell", 200, 0.47, 1.40, "2026-01-15T17:45:00Z", taker=True),
    fill(E, "no", "buy", 250, 0.70, 0.95, "2026-01-15T19:20:00Z"),
]
settlements = [
    {"ticker": A, "market_result": "no", "value": 0, "revenue": 0, "settled_time": "2025-11-22T23:00:00Z"},
    {"ticker": B, "market_result": "no", "value": 0, "revenue": 0, "settled_time": "2026-01-11T01:00:00Z"},
    {"ticker": C, "market_result": "no", "value": 0, "revenue": 30000, "settled_time": "2026-01-15T03:00:00Z"},
]
marks = {D: 0.45, E: 0.66, F: 0.12}       # current YES prices for open markets
# G settled before the live settlements window: only the archive has it
historical_markets = [{"ticker": G, "status": "finalized", "result": "yes",
                       "settlement_value_dollars": "1.0000", "close_time": "2025-11-08T23:00:00Z"}]
settle = {s["ticker"]: (1.0 if s["market_result"] == "yes" else 0.0) for s in settlements}
settle[G] = 1.0

all_fills = historical + live
markets = {}
for tk in (A, B, C, D, E, F, G):
    markets[tk] = compute_market(tk, [f for f in all_fills if f["ticker"] == tk], settle.get(tk))

positions = []
for tk, m in markets.items():
    if tk == G:
        continue           # past the positions endpoint's retention window
    pos, fees = m.position, m.fees
    if tk == F:
        pos -= 30          # planted: one 30-contract fill missing from our record
    if tk == A:
        fees += 0.25       # planted: exchange charged more than the fills show
    exposure = (m.avg_cost * pos if pos > 0 else (1 - m.avg_cost) * -pos) if pos else 0.0
    positions.append({
        "ticker": tk, "position_fp": f"{pos:.2f}",
        "realized_pnl_dollars": f"{m.realized + m.settled_pnl:.4f}",
        "fees_paid_dollars": f"{fees:.4f}", "market_exposure_dollars": f"{exposure:.4f}",
    })

deposits = [
    {"amount_cents": 100000, "status": "applied", "created_ts": "2025-10-28T12:00:00Z"},
    {"amount_cents": 50000, "status": "applied", "created_ts": "2025-12-02T12:00:00Z"},
    {"amount_cents": 25000, "status": "pending", "created_ts": "2026-01-15T21:00:00Z"},
]
withdrawals = [{"amount_cents": 20000, "status": "applied", "created_ts": "2026-01-05T12:00:00Z"}]
net_deposits = 1000 + 500 - 200

realized_net = sum(m.realized_net for m in markets.values())
open_cost = sum(m.open_cost_basis for m in markets.values())
mark_value = sum((marks[tk] * m.position if m.position > 0 else (1 - marks[tk]) * -m.position)
                 for tk, m in markets.items() if m.position)
cash = net_deposits + realized_net - open_cost + INTEREST

fixture = {
    "_note": "Synthetic sample account. All tickers, prices, sizes and balances are invented.",
    "as_of": AS_OF, "as_of_ts": AS_OF_TS,
    "balance": {"balance": round(cash * 100), "portfolio_value": round(mark_value * 100)},
    "deposits": deposits, "withdrawals": withdrawals,
    "fills": live, "historical_fills": historical,
    "settlements": settlements, "historical_markets": historical_markets,
    "positions": positions,
    "resting_orders": [{"order_id": "demo-order-900", "ticker": E, "side": "no", "action": "buy",
                        "initial_count_fp": "100.00", "yes_price_dollars": "0.6800",
                        "type": "limit", "created_time": "2026-01-15T19:30:00Z"}],
    # yesterday's stored run, so the statement can show a daily P&L
    "prior_run": {"run_ts": AS_OF_TS - 86400, "equity": round(cash + mark_value - 41.37, 2),
                  "net_deposits": net_deposits, "cash_identity_residual": INTEREST_YESTERDAY},
}

out = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                   "back_office", "fixtures", "demo_account.json")
os.makedirs(os.path.dirname(out), exist_ok=True)
with open(out, "w") as fh:
    json.dump(fixture, fh, indent=2)
print(f"wrote {out}: cash ${cash:,.2f}, positions ${mark_value:,.2f}, "
      f"lifetime P&L ${cash + mark_value - net_deposits:+,.2f}")
