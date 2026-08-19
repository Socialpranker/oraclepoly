"""Тесты Redis-backed orderbook ringbuffer + backward-window сборки.

Контекст: ringbuffer должен быть в Redis (не in-memory), т.к. WSS пишет в
backend-процессе, а record_event читает в worker-процессе. См. план
docs/superpowers/plans/2026-06-04-whale-shock-snapshot-ringbuffer.md.
"""

import asyncio
import json
from unittest.mock import AsyncMock, patch

import pytest

from app.services import snapshot_recorder as sr


@pytest.mark.asyncio
async def test_ring_push_lpush_trim_expire():
    """ring_push кладёт snapshot в Redis-list с ts_unix, trim и expire."""
    fake_redis = AsyncMock()
    with patch.object(sr, "_get_redis", return_value=fake_redis):
        snap = {
            "best_bid": 0.4,
            "best_ask": 0.42,
            "spread": 0.05,
            "depth_bid": 1000.0,
            "depth_ask": 900.0,
            "levels": {"bids": [], "asks": []},
        }
        await sr.ring_push("tok123", snap, ts_unix=1000.0)

    fake_redis.lpush.assert_awaited_once()
    key, payload = fake_redis.lpush.await_args.args
    assert key == "obs_ring:tok123"
    decoded = json.loads(payload)
    assert decoded["ts_unix"] == 1000.0
    assert decoded["best_bid"] == 0.4
    fake_redis.ltrim.assert_awaited_once_with("obs_ring:tok123", 0, sr.RING_MAXLEN - 1)
    fake_redis.expire.assert_awaited_once_with("obs_ring:tok123", sr.RING_TTL_SECONDS)


@pytest.mark.asyncio
async def test_ring_get_recent_filters_by_since_ts():
    """ring_get_recent возвращает только snapshots с ts_unix >= since_ts."""
    fake_redis = AsyncMock()
    items = [
        json.dumps({"ts_unix": 1050.0, "best_bid": 0.5}),
        json.dumps({"ts_unix": 1010.0, "best_bid": 0.4}),
        json.dumps({"ts_unix": 990.0, "best_bid": 0.3}),  # старше since_ts
    ]
    fake_redis.lrange = AsyncMock(return_value=items)
    with patch.object(sr, "_get_redis", return_value=fake_redis):
        result = await sr.ring_get_recent("tok123", since_ts=1000.0)

    fake_redis.lrange.assert_awaited_once_with("obs_ring:tok123", 0, sr.RING_MAXLEN - 1)
    assert len(result) == 2
    assert {r["ts_unix"] for r in result} == {1050.0, 1010.0}


@pytest.mark.asyncio
async def test_ring_get_recent_empty_when_no_redis():
    """Нет Redis → пустой список, без исключений."""
    with patch.object(sr, "_get_redis", return_value=None):
        result = await sr.ring_get_recent("tok123", since_ts=1000.0)
    assert result == []


@pytest.mark.asyncio
async def test_record_event_collects_backward_window():
    """record_event синхронно читает backward-окно из ringbuffer и
    передаёт его в forward-таск как prefill."""
    backward_snaps = [
        {
            "ts_unix": 1000.0,
            "best_bid": 0.40,
            "best_ask": 0.42,
            "midpoint": 0.41,
            "spread": 0.05,
            "depth_bid": 1000.0,
            "depth_ask": 900.0,
            "levels": {"bids": [], "asks": []},
            "updated_at": "2026-06-04T00:00:00+00:00",
        },
    ]

    captured = {}

    async def fake_forward(**kwargs):
        captured["prefill_rows"] = kwargs.get("prefill_rows")
        return 0

    with (
        patch.object(sr, "ring_get_recent", new=AsyncMock(return_value=backward_snaps)),
        patch.object(sr, "_collect_forward_window", new=fake_forward),
    ):
        event_id = await sr.record_event(
            trigger_type="whale_trade",
            market_id="11111111-1111-1111-1111-111111111111",
            condition_id="0xabc",
            token_ids=["tok_bw_1"],
            trigger_meta={"amount_usd": 60000.0},
        )
        # record_event спавнит _collect_forward_window через create_task —
        # отдаём управление loop'у, чтобы запланированная задача исполнилась.
        await asyncio.sleep(0)

    assert event_id is not None
    # backward snapshot превратился в готовую DB-row tuple
    assert captured["prefill_rows"] is not None
    assert len(captured["prefill_rows"]) == 1
    row = captured["prefill_rows"][0]
    # _microstructure_to_snapshot_row layout:
    # (event_id, trigger_type, trigger_meta, market_id, condition_id, token_id,
    #  captured_at, t_offset_ms, best_bid, ...) — best_bid на индексе 8
    assert row[8] == 0.40
    # t_offset_ms отрицательный (снимок ДО события)
    assert row[7] < 0


# ──────────────────────────────────────────────────────────────────────────
# REST-фоллбэк: когда ring пуст для token_id, добираем 1 снимок через CLOB /book.
# Контекст: whale-shock на средне-ликвидном рынке часто не имеет ring-снимков
# (TTL 60s × отсутствие свежих WSS book-updates). Без фоллбэка снимков 0.
# ──────────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_rest_fallback_row_builds_snapshot_from_book():
    """_rest_fallback_row: REST /book → microstructure → DB-row на event_t0."""
    from datetime import UTC, datetime

    book = {
        "bids": [{"price": "0.40", "size": "1000"}],
        "asks": [{"price": "0.42", "size": "900"}],
    }
    fake_poller = AsyncMock()
    fake_poller.fetch_order_book_rest = AsyncMock(return_value=book)

    event_t0 = datetime(2026, 6, 8, 12, 0, 0, tzinfo=UTC)
    with patch.object(sr, "get_poller_service", return_value=fake_poller):
        row = await sr._rest_fallback_row(
            event_id=__import__("uuid").UUID("22222222-2222-2222-2222-222222222222"),
            trigger_type="whale_trade",
            trigger_meta={"amount_usd": 30000.0},
            market_id="11111111-1111-1111-1111-111111111111",
            condition_id="0xabc",
            token_id="tok_rest_1",
            event_t0=event_t0,
        )

    fake_poller.fetch_order_book_rest.assert_awaited_once_with("tok_rest_1")
    assert row is not None
    # layout: (..., token_id[5], captured_at[6], t_offset_ms[7], best_bid[8], best_ask[9], midpoint[10], spread[11])
    assert row[5] == "tok_rest_1"
    assert row[6] == event_t0  # снимок проставлен на момент события
    assert row[7] == 0  # t_offset_ms ≈ 0 (REST в момент t0)
    assert row[8] == 0.40  # best_bid из book
    assert row[9] == 0.42  # best_ask из book


@pytest.mark.asyncio
async def test_rest_fallback_row_none_on_empty_book():
    """Пустой/невалидный book → None (нечего записывать), без исключений."""
    from datetime import UTC, datetime

    fake_poller = AsyncMock()
    fake_poller.fetch_order_book_rest = AsyncMock(return_value=None)

    with patch.object(sr, "get_poller_service", return_value=fake_poller):
        row = await sr._rest_fallback_row(
            event_id=__import__("uuid").UUID("22222222-2222-2222-2222-222222222222"),
            trigger_type="whale_trade",
            trigger_meta=None,
            market_id="m1",
            condition_id="0xabc",
            token_id="tok_empty",
            event_t0=datetime(2026, 6, 8, 12, 0, 0, tzinfo=UTC),
        )
    assert row is None


@pytest.mark.asyncio
async def test_record_event_rest_fallback_only_for_empty_ring_tokens():
    """record_event: ring дал снимки для tok_full → НЕ фоллбэк; ring пуст
    для tok_empty → фоллбэк-row добавляется в prefill."""
    backward_for_full = [
        {
            "ts_unix": 1000.0,
            "best_bid": 0.50,
            "best_ask": 0.52,
            "midpoint": 0.51,
            "spread": 0.04,
            "depth_bid": 1000.0,
            "depth_ask": 900.0,
            "levels": {"bids": [], "asks": []},
        },
    ]

    async def fake_ring_get_recent(token_id, *, since_ts):
        return backward_for_full if token_id == "tok_full" else []

    fallback_calls = []

    async def fake_fallback(**kwargs):
        fallback_calls.append(kwargs["token_id"])
        # вернём фиктивную row только для empty-токена
        return ("evt", "whale_trade", None, "m1", "0xabc", kwargs["token_id"], None, 0, 0.30, 0.32, 0.31, 0.06, "[]", "[]", None, None)

    captured = {}

    async def fake_forward(**kwargs):
        captured["prefill_rows"] = kwargs.get("prefill_rows")
        return 0

    with (
        patch.object(sr, "ring_get_recent", new=fake_ring_get_recent),
        patch.object(sr, "_rest_fallback_row", new=fake_fallback),
        patch.object(sr, "_collect_forward_window", new=fake_forward),
    ):
        await sr.record_event(
            trigger_type="whale_trade",
            market_id="m1",
            condition_id="0xabc",
            token_ids=["tok_full", "tok_empty"],
            trigger_meta={"amount_usd": 30000.0},
        )
        await asyncio.sleep(0)

    # фоллбэк вызван РОВНО для пустого токена
    assert fallback_calls == ["tok_empty"]
    rows = captured["prefill_rows"]
    token_ids_in_rows = {r[5] for r in rows}
    assert "tok_full" in token_ids_in_rows  # из ring
    assert "tok_empty" in token_ids_in_rows  # из REST-фоллбэка
