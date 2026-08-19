"""Тесты для app/api/d45.py — D45 stats endpoint."""

from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.api.d45 import router as d45_router
from app.core.auth import check_admin
from app.services.binance_stream_service import BinanceTick
from app.services.pyth_stream_service import PythTick


@pytest.fixture
def app_with_d45():
    """FastAPI с D45 router и подменённым check_admin (always allow)."""
    app = FastAPI()
    app.include_router(d45_router, prefix="/api/d45")
    app.dependency_overrides[check_admin] = lambda: {"user": "test-admin"}
    return app


def _pyth_tick(symbol: str, price: float, age_ms: int = 100) -> PythTick:
    now = 1779354900_000
    return PythTick(
        symbol=symbol,
        price=price,
        publish_time_ms=now - 200,
        received_at_ms=now - age_ms,
    )


def _binance_tick(symbol: str, price: float, age_ms: int = 100) -> BinanceTick:
    now = 1779354900_000
    return BinanceTick(
        symbol=symbol,
        price=price,
        trade_time_ms=now - 100,
        received_at_ms=now - age_ms,
    )


def test_stats_endpoint_returns_full_schema(app_with_d45):
    fake_pyth = type(
        "P",
        (),
        {
            "is_connected": True,
            "get_latest": lambda self, s: _pyth_tick(s, 77000.0) if s == "BTC/USD" else None,
        },
    )()
    fake_binance = type(
        "B",
        (),
        {
            "is_connected": True,
            "get_latest": lambda self, s: _binance_tick(s, 77050.0) if s == "BTC/USD" else None,
        },
    )()

    with (
        patch("app.api.d45.get_pyth_stream", return_value=fake_pyth),
        patch("app.api.d45.get_binance_stream", return_value=fake_binance),
        patch("app.api.d45.fetchval", new_callable=AsyncMock, side_effect=[5, 42]),
    ):
        client = TestClient(app_with_d45)
        resp = client.get("/api/d45/stats")

    assert resp.status_code == 200
    data = resp.json()
    # Top-level keys
    assert set(data.keys()) >= {
        "enabled",
        "scan_interval_sec",
        "pyth",
        "binance",
        "basis_pct",
        "signals",
    }
    # Pyth status
    assert data["pyth"]["connected"] is True
    assert data["pyth"]["symbols"]["BTC/USD"]["fresh"] is True
    assert data["pyth"]["symbols"]["BTC/USD"]["price"] == pytest.approx(77000.0)
    # Other symbols → not fresh
    assert data["pyth"]["symbols"]["ETH/USD"]["fresh"] is False
    # Binance same
    assert data["binance"]["connected"] is True
    assert data["binance"]["symbols"]["BTC/USD"]["fresh"] is True
    # Basis computed
    expected_basis = abs(77000.0 - 77050.0) / 77050.0 * 100.0
    assert data["basis_pct"]["BTC/USD"] == pytest.approx(expected_basis, abs=0.001)
    # ETH basis None (no binance data)
    assert data["basis_pct"]["ETH/USD"] is None
    # Signals counters
    assert data["signals"]["last_1h"] == 5
    assert data["signals"]["last_24h"] == 42


def test_stats_endpoint_handles_disconnected_streams(app_with_d45):
    fake_pyth = type(
        "P", (), {"is_connected": False, "get_latest": lambda self, s: None}
    )()
    fake_binance = type(
        "B", (), {"is_connected": False, "get_latest": lambda self, s: None}
    )()

    with (
        patch("app.api.d45.get_pyth_stream", return_value=fake_pyth),
        patch("app.api.d45.get_binance_stream", return_value=fake_binance),
        patch("app.api.d45.fetchval", new_callable=AsyncMock, side_effect=[0, 0]),
    ):
        client = TestClient(app_with_d45)
        resp = client.get("/api/d45/stats")

    assert resp.status_code == 200
    data = resp.json()
    assert data["pyth"]["connected"] is False
    assert data["binance"]["connected"] is False
    # Все symbols not fresh
    assert all(not v["fresh"] for v in data["pyth"]["symbols"].values())
    assert all(b is None for b in data["basis_pct"].values())
    assert data["signals"]["last_1h"] == 0


def test_stats_endpoint_requires_admin():
    """Без override check_admin → 401."""
    app = FastAPI()
    app.include_router(d45_router, prefix="/api/d45")
    client = TestClient(app)
    resp = client.get("/api/d45/stats")
    assert resp.status_code in (401, 403)
