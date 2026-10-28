import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def make_fill(side, qty, yes_price, fee=0.0, t="2026-01-01T00:00:00Z", action="buy",
              ticker="KXEXAMPLE-26JAN01-A", fill_id=None):
    """A fill shaped like Kalshi's V3 /portfolio/fills records (decimal strings)."""
    return {
        "fill_id": fill_id or f"f-{ticker}-{t}-{side}-{qty}",
        "ticker": ticker, "action": action, "side": side, "outcome_side": side,
        "count_fp": f"{qty:.2f}", "yes_price_dollars": f"{yes_price:.4f}",
        "no_price_dollars": f"{1 - yes_price:.4f}", "fee_cost": f"{fee:.4f}",
        "created_time": t,
    }
