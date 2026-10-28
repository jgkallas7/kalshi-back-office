"""The API client, fully offline: a fake HTTP session stands in for Kalshi."""
import base64

import pytest
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding, rsa

from back_office.client import KalshiClient, KalshiError


@pytest.fixture(scope="module")
def key():
    return rsa.generate_private_key(public_exponent=65537, key_size=2048)


def pem(key):
    return key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
                             serialization.NoEncryption())


class FakeResponse:
    def __init__(self, payload, status=200, headers=None):
        self._payload, self.status_code, self.headers = payload, status, headers or {}
        self.text = str(payload)

    def json(self):
        return self._payload


class FakeSession:
    """Serves queued responses per path and records every request."""
    def __init__(self, routes):
        self.routes = {k: list(v) for k, v in routes.items()}
        self.calls = []

    def get(self, url, headers=None, params=None, timeout=None):
        path = url.split(".com", 1)[-1].split(".co", 1)[-1]
        self.calls.append((path, dict(params or {}), headers))
        return self.routes[path].pop(0)


def client(key, routes):
    return KalshiClient(key_id="test-key-id", private_key_pem=pem(key), env="demo",
                        session=FakeSession(routes), min_interval_s=0)


def test_signature_verifies_and_ignores_the_query_string(key):
    c = client(key, {})
    sig = c.sign("1700000000000", "GET", "/trade-api/v2/portfolio/fills?limit=200")
    key.public_key().verify(
        base64.b64decode(sig), b"1700000000000GET/trade-api/v2/portfolio/fills",
        padding.PSS(mgf=padding.MGF1(hashes.SHA256()), salt_length=padding.PSS.DIGEST_LENGTH),
        hashes.SHA256())


def test_auth_headers_are_sent(key):
    c = client(key, {"/trade-api/v2/portfolio/balance":
                     [FakeResponse({"balance": 1234, "portfolio_value": 500})]})
    assert c.get_balance() == (1234, 500)
    _, _, headers = c.session.calls[0]
    assert headers["KALSHI-ACCESS-KEY"] == "test-key-id"
    assert headers["KALSHI-ACCESS-SIGNATURE"] and headers["KALSHI-ACCESS-TIMESTAMP"]


def test_paginate_follows_the_cursor_and_tolerates_null(key):
    path = "/trade-api/v2/portfolio/settlements"
    c = client(key, {path: [
        FakeResponse({"settlements": [{"ticker": "A"}, {"ticker": "B"}], "cursor": "c1"}),
        FakeResponse({"settlements": [{"ticker": "C"}], "cursor": "c2"}),
        FakeResponse({"settlements": None, "cursor": ""}),
    ]})
    assert [s["ticker"] for s in c.get_settlements()] == ["A", "B", "C"]
    cursors = [p.get("cursor") for _, p, _ in c.session.calls]
    assert cursors == [None, "c1", "c2"]
    assert all(p["limit"] == 200 for _, p, _ in c.session.calls)


def test_live_and_historical_fills_are_merged_without_duplicates(key):
    c = client(key, {
        "/trade-api/v2/portfolio/fills": [FakeResponse({"fills": [{"fill_id": "1"}, {"fill_id": "2"}]})],
        "/trade-api/v2/historical/fills": [FakeResponse({"fills": [{"fill_id": "2"}, {"fill_id": "0"}]})],
    })
    assert sorted(f["fill_id"] for f in c.get_fills()) == ["0", "1", "2"]


def test_historical_markets_are_looked_up_in_ticker_batches(key):
    path = "/trade-api/v2/historical/markets"
    c = client(key, {path: [FakeResponse({"markets": [{"ticker": "A"}]}),
                            FakeResponse({"markets": [{"ticker": "C"}]})]})
    got = c.get_historical_markets(["A", "B", "C"], batch=2)
    assert [m["ticker"] for m in got] == ["A", "C"]
    assert [p["tickers"] for _, p, _ in c.session.calls] == ["A,B", "C"]


def test_single_market_lookups_pass_the_ticker(key):
    c = client(key, {"/trade-api/v2/portfolio/positions": [FakeResponse({"market_positions": []})],
                     "/trade-api/v2/portfolio/fills": [FakeResponse({"fills": []})]})
    c.get_positions(ticker="T")
    c.get_fills(ticker="T", include_historical=False)
    assert [p.get("ticker") for _, p, _ in c.session.calls] == ["T", "T"]


def test_positions_request_includes_settled_markets(key):
    path = "/trade-api/v2/portfolio/positions"
    c = client(key, {path: [FakeResponse({"market_positions": []})]})
    c.get_positions()
    assert c.session.calls[0][1]["settlement_status"] == "all"


def test_rate_limit_is_retried_then_errors_surface(key, monkeypatch):
    monkeypatch.setattr("back_office.client.time.sleep", lambda s: None)
    path = "/trade-api/v2/portfolio/balance"
    c = client(key, {path: [FakeResponse({}, 429, {"Retry-After": "1"}),
                            FakeResponse({"balance": 1, "portfolio_value": 2})]})
    assert c.get_balance() == (1, 2)
    c2 = client(key, {path: [FakeResponse({"error": "nope"}, 500)]})
    with pytest.raises(KalshiError):
        c2.get_balance()


def test_missing_credentials_give_a_clear_error(monkeypatch):
    monkeypatch.delenv("KALSHI_API_KEY_ID", raising=False)
    monkeypatch.delenv("KALSHI_PRIVATE_KEY_PATH", raising=False)
    with pytest.raises(KalshiError, match="demo"):
        KalshiClient()
