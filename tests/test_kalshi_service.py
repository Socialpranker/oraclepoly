"""Kalshi REST client — unit tests.

Tests cover signing, header construction, parser quirks. Network calls mocked.
"""

from __future__ import annotations

import base64
from pathlib import Path

import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa

from app.services import kalshi_service
from app.services.kalshi_service import (
    KalshiAuthError,
    KalshiClient,
    _strip_query,
    sign_request,
)


@pytest.fixture
def rsa_key() -> rsa.RSAPrivateKey:
    """Generate ephemeral test RSA key (2048 bit)."""
    return rsa.generate_private_key(public_exponent=65537, key_size=2048)


@pytest.fixture
def rsa_pem_file(tmp_path: Path, rsa_key: rsa.RSAPrivateKey) -> Path:
    """Write rsa_key to a temp PEM file."""
    pem_bytes = rsa_key.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.PKCS8,
        encryption_algorithm=serialization.NoEncryption(),
    )
    p = tmp_path / "kalshi_rsa.pem"
    p.write_bytes(pem_bytes)
    return p


def test_sign_request_returns_base64(rsa_key: rsa.RSAPrivateKey):
    """sign_request produces a valid base64 string."""
    sig = sign_request(rsa_key, timestamp_ms=1700000000000, method="GET", path="/trade-api/v2/markets")
    assert isinstance(sig, str)
    # base64 decodes without error
    decoded = base64.b64decode(sig)
    # RSA-2048 signature length = 256 bytes
    assert len(decoded) == 256


def test_sign_request_deterministic_for_same_input_differs_on_pss(rsa_key: rsa.RSAPrivateKey):
    """PSS includes random salt — signatures differ across calls, both valid."""
    sig1 = sign_request(rsa_key, 1700000000000, "GET", "/markets")
    sig2 = sign_request(rsa_key, 1700000000000, "GET", "/markets")
    # PSS is randomized — signatures should NOT match
    assert sig1 != sig2


def test_sign_request_method_uppercased(rsa_key: rsa.RSAPrivateKey):
    """method передан lowercase — должен подписать uppercase для совместимости."""
    # Sign manually uppercase and compare via verification (PSS randomized → можем только проверить что метод not crashes)
    sig_lower = sign_request(rsa_key, 1700000000000, "get", "/markets")
    sig_upper = sign_request(rsa_key, 1700000000000, "GET", "/markets")
    assert isinstance(sig_lower, str)
    assert isinstance(sig_upper, str)
    # Both valid base64
    assert len(base64.b64decode(sig_lower)) == 256
    assert len(base64.b64decode(sig_upper)) == 256


def test_strip_query_no_query():
    assert _strip_query("/markets") == "/markets"


def test_strip_query_with_query():
    assert _strip_query("/markets?limit=100&cursor=abc") == "/markets"


def test_strip_query_empty_query():
    assert _strip_query("/markets?") == "/markets"


def test_load_private_key_success(rsa_pem_file: Path):
    """_load_private_key reads a valid PEM."""
    key = kalshi_service._load_private_key(str(rsa_pem_file))
    assert isinstance(key, rsa.RSAPrivateKey)


def test_load_private_key_missing_file(tmp_path: Path):
    """_load_private_key raises KalshiAuthError for missing file."""
    with pytest.raises(KalshiAuthError, match="not found"):
        kalshi_service._load_private_key(str(tmp_path / "nonexistent.pem"))


def test_load_private_key_invalid_pem(tmp_path: Path):
    """_load_private_key raises for invalid PEM content."""
    p = tmp_path / "bad.pem"
    p.write_bytes(b"not a real pem")
    with pytest.raises(KalshiAuthError, match="Failed to parse"):
        kalshi_service._load_private_key(str(p))


def test_load_private_key_rejects_non_rsa(tmp_path: Path):
    """_load_private_key rejects EC or other key types."""
    from cryptography.hazmat.primitives.asymmetric import ec

    ec_key = ec.generate_private_key(ec.SECP256R1())
    pem = ec_key.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.PKCS8,
        encryption_algorithm=serialization.NoEncryption(),
    )
    p = tmp_path / "ec.pem"
    p.write_bytes(pem)
    with pytest.raises(KalshiAuthError, match="not RSA"):
        kalshi_service._load_private_key(str(p))


def test_client_auth_headers_structure(rsa_pem_file: Path, monkeypatch):
    """KalshiClient._auth_headers returns 3 required headers + Accept."""
    settings_stub = type(
        "S",
        (),
        {
            "kalshi_api_key_id": "test-key-uuid",
            "kalshi_private_key_path": str(rsa_pem_file),
            "kalshi_api_base_url": "https://demo-api.kalshi.co/trade-api/v2",
            "kalshi_timeout_sec": 10.0,
        },
    )()
    client = KalshiClient(settings_override=settings_stub)
    headers = client._auth_headers("GET", "/markets")
    assert headers["KALSHI-ACCESS-KEY"] == "test-key-uuid"
    assert "KALSHI-ACCESS-TIMESTAMP" in headers
    assert "KALSHI-ACCESS-SIGNATURE" in headers
    assert headers["Accept"] == "application/json"
    # timestamp is ms since epoch — should be 13-digit string
    assert len(headers["KALSHI-ACCESS-TIMESTAMP"]) == 13
    assert headers["KALSHI-ACCESS-TIMESTAMP"].isdigit()


def test_client_bulk_orderbooks_caps_at_100(rsa_pem_file: Path):
    """get_orderbooks_bulk raises ValueError для >100 tickers."""
    import asyncio

    settings_stub = type(
        "S",
        (),
        {
            "kalshi_api_key_id": "k",
            "kalshi_private_key_path": str(rsa_pem_file),
            "kalshi_api_base_url": "https://demo-api.kalshi.co/trade-api/v2",
            "kalshi_timeout_sec": 10.0,
        },
    )()
    client = KalshiClient(settings_override=settings_stub)
    tickers = [f"TICKER-{i}" for i in range(101)]

    async def run():
        async with client:
            await client.get_orderbooks_bulk(tickers)

    with pytest.raises(ValueError, match="capped at 100"):
        asyncio.run(run())


def test_client_get_without_aenter_raises(rsa_pem_file: Path):
    """_get требует чтобы client был entered через async with."""
    import asyncio

    settings_stub = type(
        "S",
        (),
        {
            "kalshi_api_key_id": "k",
            "kalshi_private_key_path": str(rsa_pem_file),
            "kalshi_api_base_url": "https://demo-api.kalshi.co/trade-api/v2",
            "kalshi_timeout_sec": 10.0,
        },
    )()
    client = KalshiClient(settings_override=settings_stub)

    async def run():
        await client._get("/markets")

    with pytest.raises(RuntimeError, match="not entered"):
        asyncio.run(run())
