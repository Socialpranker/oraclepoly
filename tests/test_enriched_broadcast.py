"""Tests for enriched transaction WebSocket broadcast."""

import pytest


@pytest.mark.asyncio
async def test_enriched_payload_has_whale_profile():
    """Broadcast payload содержит профиль кита."""
    from app.tasks.transaction import build_enriched_transaction_payload

    tx = {"id": "tx-1", "direction": "buy", "amount": 25000, "outcome": "YES", "market_id": "m-1", "price": 0.65}
    whale = {
        "id": "w-1",
        "address": "0xabc",
        "wins": 10,
        "losses": 3,
        "roi_percent": 34.5,
        "total_volume": 500000,
        "pnl_usd": 180000,
        "expert_weight": 0.8,
        "category_expertise": "politics",
    }
    market = {
        "question": "Will Trump win?",
        "category": "politics",
        "current_price": 0.65,
        "oracle_score": 0.68,
        "volume_24h": 450000,
    }
    anomaly = {
        "is_anomalous": False,
        "flags": [],
        "risk_label": "clean",
        "account_age_days": 90,
        "unique_markets": 15,
        "total_trades": 50,
        "volume_concentration": 0.2,
    }

    result = build_enriched_transaction_payload(tx, whale, market, anomaly)

    assert result["whale"]["win_rate"] == pytest.approx(0.769, rel=0.01)
    assert result["whale"]["address"] == "0xabc"
    assert result["whale"]["is_followed"] is None
    assert result["anomaly"]["is_anomalous"] is False
    assert result["market_context"]["oracle_score"] == 0.68


@pytest.mark.asyncio
async def test_enriched_payload_anomalous():
    """Broadcast payload с аномальным аккаунтом."""
    from app.tasks.transaction import build_enriched_transaction_payload

    tx = {"id": "tx-2", "direction": "buy", "amount": 50000, "outcome": "YES", "market_id": "m-1", "price": 0.7}
    whale = {
        "id": "w-2",
        "address": "0xnew",
        "wins": 0,
        "losses": 0,
        "total_volume": 50000,
        "pnl_usd": 0,
        "roi_percent": 0,
        "expert_weight": 0.5,
        "category_expertise": None,
    }
    market = {
        "question": "Test?",
        "category": "crypto",
        "current_price": 0.7,
        "oracle_score": 0.5,
        "volume_24h": 100000,
    }
    anomaly = {
        "is_anomalous": True,
        "flags": ["new_account", "single_market"],
        "risk_label": "suspicious_new",
        "account_age_days": 1,
        "unique_markets": 1,
        "total_trades": 1,
        "volume_concentration": 1.0,
    }

    result = build_enriched_transaction_payload(tx, whale, market, anomaly)

    assert result["anomaly"]["is_anomalous"] is True
    assert result["anomaly"]["risk_label"] == "suspicious_new"
    assert result["category"] == "crypto"


def test_enriched_payload_none_market():
    """Если market=None -- market_context заполняется дефолтами."""
    from app.tasks.transaction import build_enriched_transaction_payload

    tx = {"id": "tx-3", "direction": "sell", "amount": 5000, "outcome": "NO", "market_id": "m-x", "price": 0.4}
    whale = {
        "id": "w-3",
        "address": "0xwho",
        "wins": 0,
        "losses": 0,
        "total_volume": 5000,
        "pnl_usd": 0,
        "roi_percent": 0,
        "expert_weight": 0.5,
        "category_expertise": None,
    }

    result = build_enriched_transaction_payload(tx, whale, None, None)

    assert result["market_title"] is None
    assert result["market_context"]["current_price"] == 0.5
    assert result["anomaly"]["is_anomalous"] is False


def test_enriched_payload_win_rate_zero_division():
    """wins=0, losses=0 -- win_rate=0.0 (нет деления на 0)."""
    from app.tasks.transaction import build_enriched_transaction_payload

    tx = {"id": "tx-4", "direction": "buy", "amount": 1000, "outcome": "YES", "market_id": "m-1", "price": 0.5}
    whale = {
        "id": "w-4",
        "address": "0xnew2",
        "wins": 0,
        "losses": 0,
        "total_volume": 1000,
        "pnl_usd": 0,
        "roi_percent": 0,
        "expert_weight": 0.5,
        "category_expertise": None,
    }

    result = build_enriched_transaction_payload(tx, whale, None, None)

    assert result["whale"]["win_rate"] == 0.0
