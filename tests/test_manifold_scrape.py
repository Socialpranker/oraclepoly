"""manifold_scrape job — unit tests с mocked client + DB."""

from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest

from app.jobs.manifold_scrape import (
    _build_upsert_row,
    _ts_to_dt,
    run_manifold_scrape,
)


def test_ts_to_dt_none():
    assert _ts_to_dt(None) is None


def test_ts_to_dt_ms_epoch():
    """Manifold uses ms since epoch. Verify round-trip через datetime API."""
    from datetime import UTC, datetime
    expected = datetime(2026, 5, 20, 12, 0, 0, tzinfo=UTC)
    ms = int(expected.timestamp() * 1000)
    dt = _ts_to_dt(ms)
    assert dt == expected


def test_build_upsert_row_full_market():
    m = {
        "id": "manifold-abc",
        "slug": "will-trump-win",
        "creatorUsername": "user1",
        "question": "Will Trump win?",
        "textDescription": "Some context",
        "outcomeType": "BINARY",
        "isResolved": False,
        "probability": 0.55,
        "volume24Hours": 1000.0,
        "volume": 50000.0,
        "uniqueBettorCount": 230,
        "closeTime": 1800000000000,
        "createdTime": 1700000000000,
        "lastUpdatedTime": 1779000000000,
        "url": "https://manifold.markets/user1/will-trump-win",
    }
    row = _build_upsert_row(m)
    assert row[0] == "manifold-abc"
    assert row[1] == "will-trump-win"
    assert row[2] == "user1"
    assert row[3] == "Will Trump win?"
    assert row[4] == "Some context"
    assert row[5] == "BINARY"
    assert row[6] is False  # is_resolved
    assert row[7] is None  # resolution
    assert row[8] is None  # resolution_probability
    assert row[9] == 0.55
    assert row[10] == 1000.0
    assert row[11] == 50000.0
    assert row[12] == 230
    assert row[13] is not None  # closeTime parsed
    assert row[14] is not None  # createdTime parsed
    assert row[16] == "https://manifold.markets/user1/will-trump-win"


def test_build_upsert_row_resolved():
    m = {
        "id": "manifold-resolved",
        "slug": "resolved-market",
        "question": "Q?",
        "outcomeType": "BINARY",
        "isResolved": True,
        "resolution": "YES",
        "resolutionProbability": 1.0,
        "probability": 0.99,
    }
    row = _build_upsert_row(m)
    assert row[6] is True
    assert row[7] == "YES"
    assert row[8] == 1.0


def test_build_upsert_row_minimum_fields():
    """Only id/slug/question/outcomeType required."""
    m = {"id": "id1", "slug": "s", "question": "q", "outcomeType": "BINARY"}
    row = _build_upsert_row(m)
    assert row[0] == "id1"
    assert row[5] == "BINARY"
    assert row[10] is None  # volume_24h
    assert row[13] is None  # closeTime


def test_build_upsert_row_uses_description_fallback():
    """Если textDescription отсутствует, fallback на description."""
    m = {
        "id": "i", "slug": "s", "question": "q", "outcomeType": "BINARY",
        "description": "Plain description",
    }
    row = _build_upsert_row(m)
    assert row[4] == "Plain description"


@pytest.mark.asyncio
async def test_run_manifold_scrape_empty():
    """No markets → return zeros."""
    fake_client = AsyncMock()
    fake_client.list_active_binary_markets = AsyncMock(return_value=[])

    with patch("app.jobs.manifold_scrape.get_manifold_client", return_value=fake_client):
        result = await run_manifold_scrape()

    assert result == {"markets": 0, "upserted": 0, "snapshots": 0}


@pytest.mark.asyncio
async def test_run_manifold_scrape_upserts_and_snapshots():
    fake_markets = [
        {"id": "m1", "slug": "s1", "question": "Q1?", "outcomeType": "BINARY",
         "probability": 0.40, "volume24Hours": 500.0, "uniqueBettorCount": 50},
        {"id": "m2", "slug": "s2", "question": "Q2?", "outcomeType": "BINARY",
         "probability": 0.75, "volume24Hours": 1200.0, "uniqueBettorCount": 80},
    ]
    fake_client = AsyncMock()
    fake_client.list_active_binary_markets = AsyncMock(return_value=fake_markets)

    with patch("app.jobs.manifold_scrape.get_manifold_client", return_value=fake_client), \
         patch("app.jobs.manifold_scrape.execute", new=AsyncMock(return_value="INSERT 0 1")) as mock_exec, \
         patch("app.jobs.manifold_scrape.executemany", new=AsyncMock(return_value=None)) as mock_em:
        result = await run_manifold_scrape()

    assert result["markets"] == 2
    assert result["upserted"] == 2
    assert result["snapshots"] == 2
    assert mock_exec.call_count == 2
    mock_em.assert_called_once()


@pytest.mark.asyncio
async def test_run_manifold_scrape_skips_missing_probability():
    """No probability → no snapshot insert, но upsert всё равно делается."""
    fake_markets = [
        {"id": "m1", "slug": "s1", "question": "Q?", "outcomeType": "BINARY", "probability": None},
    ]
    fake_client = AsyncMock()
    fake_client.list_active_binary_markets = AsyncMock(return_value=fake_markets)

    with patch("app.jobs.manifold_scrape.get_manifold_client", return_value=fake_client), \
         patch("app.jobs.manifold_scrape.execute", new=AsyncMock(return_value="INSERT 0 1")), \
         patch("app.jobs.manifold_scrape.executemany", new=AsyncMock(return_value=None)) as mock_em:
        result = await run_manifold_scrape()

    assert result["markets"] == 1
    assert result["upserted"] == 1
    assert result["snapshots"] == 0
    mock_em.assert_not_called()


@pytest.mark.asyncio
async def test_run_manifold_scrape_continues_after_upsert_error():
    """Один market fails — продолжаем с остальными."""
    fake_markets = [
        {"id": "m1", "slug": "s1", "question": "Q?", "outcomeType": "BINARY", "probability": 0.5,
         "volume24Hours": 100.0, "uniqueBettorCount": 10},
        {"id": "m2", "slug": "s2", "question": "Q?", "outcomeType": "BINARY", "probability": 0.6,
         "volume24Hours": 200.0, "uniqueBettorCount": 20},
    ]
    fake_client = AsyncMock()
    fake_client.list_active_binary_markets = AsyncMock(return_value=fake_markets)

    call_count = {"n": 0}
    async def fake_execute(*args, **kwargs):
        call_count["n"] += 1
        if call_count["n"] == 1:
            raise RuntimeError("DB error на первом")
        return "INSERT 0 1"

    with patch("app.jobs.manifold_scrape.get_manifold_client", return_value=fake_client), \
         patch("app.jobs.manifold_scrape.execute", new=fake_execute), \
         patch("app.jobs.manifold_scrape.executemany", new=AsyncMock(return_value=None)):
        result = await run_manifold_scrape()

    assert result["markets"] == 2
    assert result["upserted"] == 1  # 1 succeeded, 1 failed
    assert result["snapshots"] == 1  # only the succeeded one добавлен в snapshot_rows? Actually both...
    # Уточнение: snapshot_rows добавляется после execute. На failed market — мы делаем
    # continue, поэтому m1 не попадает в snapshot_rows. m2 — попадает.
