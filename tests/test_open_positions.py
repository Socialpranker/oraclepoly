"""Tests for app.core.open_positions — Phase 1 Stage 2.

Pure-function tests для row → OpenPosition conversion. Без DB моков.
"""

from __future__ import annotations

from datetime import UTC, datetime

from app.core.open_positions import (
    OPEN_POSITIONS_RISK_SQL,
    _row_to_open_position,
)
from app.services.risk_engine import OpenPosition


def test_row_to_open_position_basic():
    row = {
        "market_id": "0xMARKET1",
        "event_id": "0xEVT1",
        "category": "Sports",
        "stake": 100.0,
        "created_at": datetime(2026, 5, 20, 10, 0, 0, tzinfo=UTC),
    }
    pos = _row_to_open_position(row)
    assert isinstance(pos, OpenPosition)
    assert pos.market_id == "0xMARKET1"
    assert pos.event_id == "0xEVT1"
    assert pos.category == "Sports"
    assert pos.stake == 100.0
    assert pos.created_at.tzinfo is not None
    assert pos.resolved_at is None


def test_row_to_open_position_category_fallback():
    """category=None → 'unknown' (risk_engine ожидает строку)."""
    row = {
        "market_id": "m",
        "event_id": "e",
        "category": None,
        "stake": 50.0,
        "created_at": datetime.now(UTC),
    }
    pos = _row_to_open_position(row)
    assert pos.category == "unknown"


def test_row_to_open_position_naive_datetime_coerced_to_utc():
    """psycopg может вернуть naive datetime — fallback UTC, не паникуем."""
    naive = datetime(2026, 5, 20, 10, 0, 0)
    row = {
        "market_id": "m",
        "event_id": None,
        "category": "Other",
        "stake": 25.0,
        "created_at": naive,
    }
    pos = _row_to_open_position(row)
    assert pos.created_at.tzinfo is not None
    assert pos.created_at.tzinfo == UTC


def test_row_to_open_position_event_id_optional():
    """event_id может быть None (some markets без event)."""
    row = {
        "market_id": "m",
        "event_id": None,
        "category": "Politics",
        "stake": 10.0,
        "created_at": datetime.now(UTC),
    }
    pos = _row_to_open_position(row)
    assert pos.event_id is None


# --- SQL constants ---


def test_sql_filters_live_only():
    """Query должен исключать virtual (risk_engine для real exposure)."""
    assert "execution_mode" in OPEN_POSITIONS_RISK_SQL
    assert "virtual" in OPEN_POSITIONS_RISK_SQL.lower()


def test_sql_filters_unsettled():
    """settled_at IS NULL — позиция всё ещё open."""
    assert "settled_at IS NULL" in OPEN_POSITIONS_RISK_SQL


def test_sql_filters_live_statuses():
    """status IN placed/filled (не intent, не rejected)."""
    assert "'placed'" in OPEN_POSITIONS_RISK_SQL
    assert "'filled'" in OPEN_POSITIONS_RISK_SQL


def test_sql_joins_markets_for_category_and_event():
    """JOIN markets для event_id + category."""
    assert "markets" in OPEN_POSITIONS_RISK_SQL.lower()
    assert "event_id" in OPEN_POSITIONS_RISK_SQL
    assert "category" in OPEN_POSITIONS_RISK_SQL
