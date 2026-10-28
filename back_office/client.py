"""Minimal read-only Kalshi API client: RSA-PSS request signing and pagination.

Only the portfolio endpoints the back office needs. It never places, amends or
cancels orders, so it only ever issues GET requests.

Credentials come from the environment (or a .env file if python-dotenv is
installed):
    KALSHI_API_KEY_ID        key id from Kalshi's API settings page
    KALSHI_PRIVATE_KEY_PATH  path to the matching RSA private key (PEM)
    KALSHI_ENV               "prod" (default) or "demo"
"""
from __future__ import annotations

import base64
import os
import time

import requests
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding

BASE_URLS = {
    "prod": "https://api.elections.kalshi.com",
    "demo": "https://demo-api.kalshi.co",
}
API = "/trade-api/v2"


class KalshiError(RuntimeError):
    pass


class KalshiClient:
    def __init__(self, key_id: str | None = None, private_key_pem: bytes | None = None,
                 env: str | None = None, session: requests.Session | None = None,
                 min_interval_s: float = 0.06):
        self.key_id = key_id or os.getenv("KALSHI_API_KEY_ID", "")
        if private_key_pem is None:
            path = os.getenv("KALSHI_PRIVATE_KEY_PATH", "")
            if path:
                with open(os.path.expanduser(path), "rb") as fh:
                    private_key_pem = fh.read()
        if not self.key_id or not private_key_pem:
            raise KalshiError(
                "Set KALSHI_API_KEY_ID and KALSHI_PRIVATE_KEY_PATH "
                "(or run `python -m back_office demo` to use the bundled sample account).")
        self._key = serialization.load_pem_private_key(private_key_pem, password=None)
        self.base_url = BASE_URLS[(env or os.getenv("KALSHI_ENV", "prod")).lower()]
        self.session = session or requests.Session()
        self._min_interval = min_interval_s     # stay well inside the read rate limit
        self._last_call = 0.0

    # -- transport --------------------------------------------------------------

    def sign(self, timestamp_ms: str, method: str, path: str) -> str:
        """Kalshi signs timestamp + method + path (no query string) with RSA-PSS/SHA-256."""
        message = f"{timestamp_ms}{method}{path.split('?')[0]}".encode()
        signature = self._key.sign(
            message,
            padding.PSS(mgf=padding.MGF1(hashes.SHA256()), salt_length=padding.PSS.DIGEST_LENGTH),
            hashes.SHA256(),
        )
        return base64.b64encode(signature).decode()

    def get(self, path: str, params: dict | None = None, _attempt: int = 0) -> dict:
        wait = self._min_interval - (time.monotonic() - self._last_call)
        if wait > 0:
            time.sleep(wait)
        self._last_call = time.monotonic()
        ts = str(int(time.time() * 1000))
        headers = {
            "KALSHI-ACCESS-KEY": self.key_id,
            "KALSHI-ACCESS-SIGNATURE": self.sign(ts, "GET", path),
            "KALSHI-ACCESS-TIMESTAMP": ts,
            "Accept": "application/json",
        }
        r = self.session.get(self.base_url + path, headers=headers, params=params, timeout=15)
        if r.status_code == 429 and _attempt < 3:
            retry_after = float(r.headers.get("Retry-After") or 0) or 0.5 * 2 ** _attempt
            time.sleep(min(retry_after, 5.0))
            return self.get(path, params, _attempt + 1)
        if r.status_code >= 400:
            raise KalshiError(f"GET {path} -> {r.status_code}: {r.text[:200]}")
        return r.json()

    def paginate(self, path: str, key: str, params: dict | None = None,
                 page_size: int = 200) -> list[dict]:
        """Follow Kalshi's cursor until it runs out.

        Every Kalshi list endpoint is paginated. Reading only the first page
        looks fine on a small account and silently drops records on a large one,
        so nothing in this package ever calls a list endpoint without this.
        """
        items: list[dict] = []
        cursor = None
        while True:
            p = dict(params or {}, limit=page_size)
            if cursor:
                p["cursor"] = cursor
            page = self.get(path, p)
            batch = page.get(key) or []        # Kalshi returns null, not [], when empty
            items.extend(batch)
            cursor = page.get("cursor")
            if not batch or not cursor:
                return items

    # -- portfolio endpoints ----------------------------------------------------

    def get_balance(self) -> tuple[int, int]:
        """(cash balance, mark-to-market value of open positions), both in cents."""
        b = self.get(f"{API}/portfolio/balance")
        return int(b.get("balance") or 0), int(b.get("portfolio_value") or 0)

    def get_fills(self, include_historical: bool = True) -> list[dict]:
        """All fills. Kalshi moves fills older than its live retention window to a
        separate /historical endpoint; both are merged and de-duplicated by fill_id
        so the engine always sees each market's complete history."""
        fills = self.paginate(f"{API}/portfolio/fills", "fills")
        if include_historical:
            seen = {f.get("fill_id") for f in fills}
            fills += [f for f in self.paginate(f"{API}/historical/fills", "fills")
                      if f.get("fill_id") not in seen]
        return fills

    def get_positions(self) -> list[dict]:
        """Per-market positions, including settled and flat markets."""
        return self.paginate(f"{API}/portfolio/positions", "market_positions",
                             params={"settlement_status": "all"})

    def get_settlements(self) -> list[dict]:
        return self.paginate(f"{API}/portfolio/settlements", "settlements")

    def get_deposits(self) -> list[dict]:
        return self.paginate(f"{API}/portfolio/deposits", "deposits")

    def get_withdrawals(self) -> list[dict]:
        return self.paginate(f"{API}/portfolio/withdrawals", "withdrawals")

    def get_resting_orders(self) -> list[dict]:
        return self.paginate(f"{API}/portfolio/orders", "orders", params={"status": "resting"})
