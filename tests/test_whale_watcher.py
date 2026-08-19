"""
Whale Watcher Handler Tests

Verifies idempotency and transaction processing logic.
"""

from datetime import UTC, datetime
from decimal import Decimal
from unittest.mock import AsyncMock, MagicMock, patch

import pytest


@pytest.mark.asyncio
async def test_check_transaction_exists_found():
    """Test detecting an existing transaction."""
    from app.handlers.whale_watcher import check_transaction_exists

    # fetchval returns True when transaction exists (SELECT EXISTS(...))
    with patch("app.handlers.whale_watcher.fetchval", new_callable=AsyncMock, return_value=True):
        result = await check_transaction_exists("0xabc123")

    assert result is True


@pytest.mark.asyncio
async def test_check_transaction_exists_not_found():
    """Test when transaction does not exist."""
    from app.handlers.whale_watcher import check_transaction_exists

    # fetchval returns False when transaction does not exist
    with patch("app.handlers.whale_watcher.fetchval", new_callable=AsyncMock, return_value=False):
        result = await check_transaction_exists("0xnew456")

    assert result is False


@pytest.mark.asyncio
async def test_check_transaction_and_create_whale():
    """Test processing a new transaction (simplified mock)."""
    from app.handlers.whale_watcher import check_transaction_exists, create_or_get_whale

    # Test the key idempotency function — fetchval returns False (no existing tx)
    with patch("app.handlers.whale_watcher.fetchval", new_callable=AsyncMock, return_value=False):
        result = await check_transaction_exists("0xnew-tx")
    assert result is False

    # Test whale creation — mock TransactionRepository methods.
    # get_whale_by_address использует fetchrow из transaction_repository (SELECT).
    # create_whale вызывает _create из BaseRepository, который использует fetchrow из base.
    # _create делает dict(row) — возвращаем словарь напрямую, чтобы dict() работал корректно.
    insert_result = {"id": "whale-uuid", "address": "0xwhale", "expert_weight": 0.5}
    with (
        patch(
            "app.repositories.transaction_repository.fetchrow",
            new_callable=AsyncMock,
            return_value=None,  # get_whale_by_address -> SELECT * FROM whales WHERE address = $1 -> None
        ),
        patch(
            "app.repositories.transaction_repository.record_to_dict",
            return_value=None,  # get_whale_by_address -> record_to_dict(None) -> None
        ),
        patch(
            "app.repositories.base.fetchrow",
            new_callable=AsyncMock,
            return_value=insert_result,  # create_whale -> INSERT INTO whales ... RETURNING * -> dict
        ),
    ):
        whale = await create_or_get_whale("0xwhale")
    assert whale["id"] == "whale-uuid"


@pytest.mark.asyncio
async def test_store_transaction_parses_string_timestamp():
    """store_transaction должен конвертировать ISO строку block_timestamp в datetime."""
    from app.handlers.whale_watcher import store_transaction

    mock_record = MagicMock()
    mock_record.__iter__ = lambda self: iter([("id", "tx-1")])

    captured_args = {}

    async def capture_fetchrow(query, *args):
        captured_args["args"] = args
        return mock_record

    with (
        patch("app.handlers.whale_watcher.fetchrow", side_effect=capture_fetchrow),
        patch(
            "app.handlers.whale_watcher.record_to_dict",
            return_value={"id": "tx-1"},
        ),
    ):
        await store_transaction(
            tx_hash="0xtest",
            whale_id="whale-1",
            amount=Decimal("5000"),
            direction="buy",
            outcome="YES",
            r2_archive_key="archive/test.json.gz",
            block_timestamp="2026-02-23T17:47:00+00:00",
            log_index=0,
        )

    # $10 = block_timestamp — должен быть datetime, не строкой
    block_ts_arg = captured_args["args"][9]
    assert isinstance(block_ts_arg, datetime), f"Ожидался datetime, получен {type(block_ts_arg)}"
    assert block_ts_arg == datetime(2026, 2, 23, 17, 47, 0, tzinfo=UTC)


@pytest.mark.asyncio
async def test_store_transaction_handles_none_timestamp():
    """store_transaction должен корректно обрабатывать None block_timestamp."""
    from app.handlers.whale_watcher import store_transaction

    mock_record = MagicMock()
    mock_record.__iter__ = lambda self: iter([("id", "tx-2")])

    captured_args = {}

    async def capture_fetchrow(query, *args):
        captured_args["args"] = args
        return mock_record

    with (
        patch("app.handlers.whale_watcher.fetchrow", side_effect=capture_fetchrow),
        patch(
            "app.handlers.whale_watcher.record_to_dict",
            return_value={"id": "tx-2"},
        ),
    ):
        await store_transaction(
            tx_hash="0xtest2",
            whale_id="whale-2",
            amount=Decimal("10000"),
            direction="sell",
            outcome="NO",
            r2_archive_key="archive/test2.json.gz",
            block_timestamp=None,
            log_index=0,
        )

    block_ts_arg = captured_args["args"][9]
    assert block_ts_arg is None


@pytest.mark.asyncio
async def test_maybe_trigger_awaits_record_event():
    """_maybe_trigger_event_snapshot await-ит record_event (стал async).

    record_event импортируется локально в функции — мокаем по месту
    определения (app.services.snapshot_recorder.record_event).
    """
    from app.handlers.whale_watcher import _maybe_trigger_event_snapshot

    transaction = {"direction": "BUY", "outcome": "YES", "tx_hash": "0xdead"}
    fake_row = {"condition_id": "0xcond", "clob_token_ids": '["tok1", "tok2"]'}

    with (
        patch("app.handlers.whale_watcher.fetchrow", new_callable=AsyncMock, return_value=fake_row),
        patch("app.services.snapshot_recorder.record_event", new_callable=AsyncMock, return_value="evt") as mock_rec,
    ):
        await _maybe_trigger_event_snapshot(
            transaction, Decimal("60000"), "11111111-1111-1111-1111-111111111111"
        )

    mock_rec.assert_awaited_once()
    assert mock_rec.await_args.kwargs["trigger_type"] == "whale_trade"
    assert mock_rec.await_args.kwargs["token_ids"] == ["tok1", "tok2"]
