"""Тесты D3 scan job."""

from __future__ import annotations

import time
from unittest.mock import AsyncMock, patch

import pytest


def _row(event_pid, market_pid, price, tokens=None):
    return {
        "event_pk": "uuid-event",
        "event_polymarket_id": event_pid,
        "market_polymarket_id": market_pid,
        "condition_id": f"cond-{market_pid}",
        "current_price": price,
        "clob_token_ids": tokens or [],
    }


def _fetch_mock(active_rows, open_event_ids=()):
    """Mock fetch: первый вызов — active_events SQL, второй — open_events SQL."""
    open_rows = [{"event_id": eid} for eid in open_event_ids]
    return AsyncMock(side_effect=[active_rows, open_rows])


@pytest.mark.asyncio
async def test_d3_scan_no_active_events():
    from app.jobs.d3_scan import run_d3_scan

    with patch("app.jobs.d3_scan.fetch", _fetch_mock([])):
        result = await run_d3_scan()

    assert result == {
        "events": 0,
        "markets": 0,
        "signals": 0,
        "inserted": 0,
        "skipped_open": 0,
    }


@pytest.mark.asyncio
async def test_d3_scan_uses_live_price_when_ws_cache_hit():
    """Live WS midpoint замещает 15-минутный db_price при валидном кэше."""
    from app.jobs.d3_scan import run_d3_scan

    rows = [
        _row("evt-1", "m0", 0.55, ["yes_tok_m0", "no_tok_m0"]),
        _row("evt-1", "m1", 0.40, ["yes_tok_m1", "no_tok_m1"]),
        _row("evt-1", "m2", 0.35, ["yes_tok_m2", "no_tok_m2"]),  # sum 1.30
    ]

    repo_mock = AsyncMock()
    repo_mock.insert = AsyncMock(return_value=True)

    async def _fake_cache_get(key):
        # State-кэш контракт (2026-07-03): live требует совпадения эпохи + свежий heartbeat
        if key == "microstructure:yes_tok_m0":
            return {"midpoint": 0.70, "epoch": 42.0}  # живее, чем db_price=0.55
        if key == "ws:conn_epoch":
            return {"epoch": 42.0}
        if key == "ws:last_message_at":
            return {"ts": time.time()}
        return None

    with (
        patch("app.jobs.d3_scan.fetch", _fetch_mock(rows)),
        patch("app.jobs.d3_scan.get_detector_signal_repository", return_value=repo_mock),
        patch("app.services.live_prices.cache_get", AsyncMock(side_effect=_fake_cache_get)),
    ):
        result = await run_d3_scan()

    assert result["live_priced"] == 1
    inserted_signal = repo_mock.insert.call_args[0][0]
    assert inserted_signal.market_id == "m0"


@pytest.mark.asyncio
async def test_d3_scan_emits_signal_when_sum_above_trigger():
    """Один event с sum YES > 1.20 → один Signal вставлен."""
    from app.jobs.d3_scan import run_d3_scan

    rows = [
        _row("evt-1", "m0", 0.55, ["tok0"]),
        _row("evt-1", "m1", 0.40, ["tok1"]),
        _row("evt-1", "m2", 0.35, ["tok2"]),  # sum 1.30
    ]

    repo_mock = AsyncMock()
    repo_mock.insert = AsyncMock(return_value=True)

    with (
        patch("app.jobs.d3_scan.fetch", _fetch_mock(rows)),
        patch("app.jobs.d3_scan.get_detector_signal_repository", return_value=repo_mock),
    ):
        result = await run_d3_scan()

    assert result["events"] == 1
    assert result["markets"] == 3
    assert result["signals"] == 1
    assert result["inserted"] == 1
    repo_mock.insert.assert_awaited_once()
    inserted_signal = repo_mock.insert.call_args[0][0]
    assert inserted_signal.detector == "D3_cross_mkt_arb"
    assert inserted_signal.market_id == "m0"
    assert inserted_signal.event_id == "evt-1"


@pytest.mark.asyncio
async def test_d3_scan_emits_no_token_not_yes_token():
    """D3 side=NO → token_id обязан быть NO-токеном (index 1), не YES (index 0).

    Регрессия: до фикса scan клал yes_token_id(), и в live бот купил бы YES-токен
    фаворита при намерении NO — ставка против собственного сигнала.
    clob_token_ids canonical = [yes_token, no_token] (market_sync.py:307-323).
    """
    from app.jobs.d3_scan import run_d3_scan

    rows = [
        _row("evt-1", "m0", 0.55, ["yes_tok_m0", "no_tok_m0"]),
        _row("evt-1", "m1", 0.40, ["yes_tok_m1", "no_tok_m1"]),
        _row("evt-1", "m2", 0.35, ["yes_tok_m2", "no_tok_m2"]),  # sum 1.30, target = m0
    ]

    repo_mock = AsyncMock()
    repo_mock.insert = AsyncMock(return_value=True)

    with (
        patch("app.jobs.d3_scan.fetch", _fetch_mock(rows)),
        patch("app.jobs.d3_scan.get_detector_signal_repository", return_value=repo_mock),
    ):
        await run_d3_scan()

    inserted_signal = repo_mock.insert.call_args[0][0]
    assert inserted_signal.side == "NO"
    assert inserted_signal.market_id == "m0"  # самый дорогой YES
    assert inserted_signal.token_id == "no_tok_m0"  # NO-токен target-маркета


@pytest.mark.asyncio
async def test_d3_scan_skips_event_below_trigger():
    from app.jobs.d3_scan import run_d3_scan

    rows = [
        _row("evt-1", "m0", 0.50),
        _row("evt-1", "m1", 0.40),
        _row("evt-1", "m2", 0.20),  # sum 1.10 — ниже trigger
    ]

    repo_mock = AsyncMock()
    repo_mock.insert = AsyncMock(return_value=True)

    with (
        patch("app.jobs.d3_scan.fetch", _fetch_mock(rows)),
        patch("app.jobs.d3_scan.get_detector_signal_repository", return_value=repo_mock),
    ):
        result = await run_d3_scan()

    assert result["signals"] == 0
    repo_mock.insert.assert_not_awaited()


@pytest.mark.asyncio
async def test_d3_scan_groups_by_event():
    """Markets разных events не смешиваются."""
    from app.jobs.d3_scan import run_d3_scan

    rows = [
        _row("evt-A", "a1", 0.55),
        _row("evt-A", "a2", 0.40),
        _row("evt-A", "a3", 0.35),  # sum A 1.30 → signal
        _row("evt-B", "b1", 0.30),
        _row("evt-B", "b2", 0.40),  # sum B 0.70 → нет
    ]

    repo_mock = AsyncMock()
    repo_mock.insert = AsyncMock(return_value=True)

    with (
        patch("app.jobs.d3_scan.fetch", _fetch_mock(rows)),
        patch("app.jobs.d3_scan.get_detector_signal_repository", return_value=repo_mock),
    ):
        result = await run_d3_scan()

    assert result["events"] == 2
    assert result["signals"] == 1
    inserted_signal = repo_mock.insert.call_args[0][0]
    assert inserted_signal.event_id == "evt-A"


@pytest.mark.asyncio
async def test_d3_scan_handles_insert_conflict_idempotently():
    """ON CONFLICT возвращает False — signals считаются, inserted нет."""
    from app.jobs.d3_scan import run_d3_scan

    rows = [
        _row("evt-1", "m0", 0.60),
        _row("evt-1", "m1", 0.45),
        _row("evt-1", "m2", 0.30),  # sum 1.35
    ]

    repo_mock = AsyncMock()
    repo_mock.insert = AsyncMock(return_value=False)  # дубль

    with (
        patch("app.jobs.d3_scan.fetch", _fetch_mock(rows)),
        patch("app.jobs.d3_scan.get_detector_signal_repository", return_value=repo_mock),
    ):
        result = await run_d3_scan()

    assert result["signals"] == 1
    assert result["inserted"] == 0


def test_d3_scan_sql_includes_30d_horizon_cap():
    """D3 SQL должен фильтровать markets с end_date > now() + 30 days."""
    from app.jobs.d3_scan import _ACTIVE_NEGRISK_EVENTS_SQL

    assert "INTERVAL '30 days'" in _ACTIVE_NEGRISK_EVENTS_SQL
    assert "m.end_date IS NOT NULL" in _ACTIVE_NEGRISK_EVENTS_SQL


def test_d3_scan_sql_excludes_markets_past_end_date():
    """D3 не должен входить в рынки после end_date: is_active из Gamma живёт
    до 14 дней после закрытия, поэтому нужна явная нижняя граница
    (инцидент 2026-07-16: 16 filled-ордеров после end_date за 30 дней)."""
    from app.jobs.d3_scan import _ACTIVE_NEGRISK_EVENTS_SQL

    assert "m.end_date > now()" in _ACTIVE_NEGRISK_EVENTS_SQL


@pytest.mark.asyncio
async def test_d3_scan_skips_event_with_existing_open_signal():
    """Если у event уже есть открытый сигнал — пропускаем целиком."""
    from app.jobs.d3_scan import run_d3_scan

    rows = [
        _row("evt-1", "m0", 0.55),
        _row("evt-1", "m1", 0.40),
        _row("evt-1", "m2", 0.35),  # sum 1.30 — нормально emitнул бы
    ]

    repo_mock = AsyncMock()
    repo_mock.insert = AsyncMock(return_value=True)

    with (
        patch("app.jobs.d3_scan.fetch", _fetch_mock(rows, open_event_ids=["evt-1"])),
        patch("app.jobs.d3_scan.get_detector_signal_repository", return_value=repo_mock),
    ):
        result = await run_d3_scan()

    assert result["skipped_open"] == 1
    assert result["signals"] == 0
    repo_mock.insert.assert_not_awaited()


# --- Enqueue после insert (Phase 3 Task 3, audit топ-7) -------------------
# repo.insert() возвращает bool, не id (Signal иммутабелен, id — DB-generated
# uuid). d3_scan лукапит id по natural key (detector, market_id, event_id,
# generated_at) сразу после insert и кладёт в Redis-очередь для демона.


@pytest.mark.asyncio
async def test_d3_scan_enqueues_signal_id_after_insert():
    """Успешный insert → fetchval находит id → enqueue кладёт его в очередь."""
    from app.jobs.d3_scan import run_d3_scan

    rows = [
        _row("evt-1", "m0", 0.55),
        _row("evt-1", "m1", 0.40),
        _row("evt-1", "m2", 0.35),  # sum 1.30
    ]

    repo_mock = AsyncMock()
    repo_mock.insert = AsyncMock(return_value=True)
    fetchval_mock = AsyncMock(return_value="sig-uuid-123")
    enqueue_mock = AsyncMock(return_value=True)

    with (
        patch("app.jobs.d3_scan.fetch", _fetch_mock(rows)),
        patch("app.jobs.d3_scan.get_detector_signal_repository", return_value=repo_mock),
        patch("app.jobs.d3_scan.fetchval", fetchval_mock),
        patch("app.jobs.d3_scan.enqueue", enqueue_mock),
    ):
        result = await run_d3_scan()

    assert result["inserted"] == 1
    enqueue_mock.assert_awaited_once_with(
        "oracle:queue:execution_signals", {"detector_signal_id": "sig-uuid-123"}
    )


@pytest.mark.asyncio
async def test_d3_scan_does_not_enqueue_on_duplicate_insert():
    """ON CONFLICT (дубль) → insert=False → enqueue не вызывается вовсе."""
    from app.jobs.d3_scan import run_d3_scan

    rows = [
        _row("evt-1", "m0", 0.60),
        _row("evt-1", "m1", 0.45),
        _row("evt-1", "m2", 0.30),
    ]

    repo_mock = AsyncMock()
    repo_mock.insert = AsyncMock(return_value=False)
    enqueue_mock = AsyncMock(return_value=True)

    with (
        patch("app.jobs.d3_scan.fetch", _fetch_mock(rows)),
        patch("app.jobs.d3_scan.get_detector_signal_repository", return_value=repo_mock),
        patch("app.jobs.d3_scan.enqueue", enqueue_mock),
    ):
        result = await run_d3_scan()

    assert result["inserted"] == 0
    enqueue_mock.assert_not_awaited()


@pytest.mark.asyncio
async def test_d3_scan_enqueue_failure_does_not_break_scan():
    """Redis/fetchval падает → scan не должен упасть, insert уже засчитан."""
    from app.jobs.d3_scan import run_d3_scan

    rows = [
        _row("evt-1", "m0", 0.55),
        _row("evt-1", "m1", 0.40),
        _row("evt-1", "m2", 0.35),
    ]

    repo_mock = AsyncMock()
    repo_mock.insert = AsyncMock(return_value=True)
    fetchval_mock = AsyncMock(side_effect=RuntimeError("db down"))

    with (
        patch("app.jobs.d3_scan.fetch", _fetch_mock(rows)),
        patch("app.jobs.d3_scan.get_detector_signal_repository", return_value=repo_mock),
        patch("app.jobs.d3_scan.fetchval", fetchval_mock),
    ):
        result = await run_d3_scan()

    assert result["inserted"] == 1  # insert прошёл, enqueue-сбой не откатывает


@pytest.mark.asyncio
async def test_d3_scan_enqueue_skipped_when_id_not_found():
    """fetchval возвращает None (гонка/лаг реплики) → enqueue не вызывается."""
    from app.jobs.d3_scan import run_d3_scan

    rows = [
        _row("evt-1", "m0", 0.55),
        _row("evt-1", "m1", 0.40),
        _row("evt-1", "m2", 0.35),
    ]

    repo_mock = AsyncMock()
    repo_mock.insert = AsyncMock(return_value=True)
    fetchval_mock = AsyncMock(return_value=None)
    enqueue_mock = AsyncMock(return_value=True)

    with (
        patch("app.jobs.d3_scan.fetch", _fetch_mock(rows)),
        patch("app.jobs.d3_scan.get_detector_signal_repository", return_value=repo_mock),
        patch("app.jobs.d3_scan.fetchval", fetchval_mock),
        patch("app.jobs.d3_scan.enqueue", enqueue_mock),
    ):
        result = await run_d3_scan()

    assert result["inserted"] == 1
    enqueue_mock.assert_not_awaited()
