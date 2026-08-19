from unittest.mock import AsyncMock, patch

import pytest

from app.repositories.cleanup_repository import CleanupRepository

MODULE = "app.repositories.cleanup_repository"


@pytest.mark.anyio
async def test_delete_old_transactions():
    """CleanupRepository.delete_old_transactions возвращает количество удалённых строк."""
    with patch(f"{MODULE}.execute", new_callable=AsyncMock, return_value="DELETE 2") as mock_exec:
        repo = CleanupRepository()
        count = await repo.delete_old_transactions(days=7, max_amount=1000, is_whale=False)

    assert count == 2
    mock_exec.assert_called_once()
    sql = mock_exec.call_args[0][0]
    assert "DELETE FROM transactions" in sql
    assert "created_at < $1" in sql
    assert "amount < $2" in sql
    assert "is_whale = $3" in sql


@pytest.mark.anyio
async def test_delete_old_transactions_error_returns_zero():
    """При ошибке execute → возвращает 0."""
    with patch(f"{MODULE}.execute", new_callable=AsyncMock, side_effect=Exception("DB error")):
        repo = CleanupRepository()
        count = await repo.delete_old_transactions(days=7)

    assert count == 0


@pytest.mark.anyio
async def test_deactivate_expired_markets():
    """CleanupRepository.deactivate_expired_markets деактивирует просроченные рынки."""
    with patch(f"{MODULE}.execute", new_callable=AsyncMock, return_value="UPDATE 450") as mock_exec:
        repo = CleanupRepository()
        count = await repo.deactivate_expired_markets()

    assert count == 450
    sql = mock_exec.call_args[0][0]
    assert "UPDATE markets" in sql
    assert "is_active = false" in sql
    assert "end_date < $1" in sql


@pytest.mark.anyio
async def test_deactivate_expired_markets_error_returns_zero():
    """При ошибке execute → возвращает 0."""
    with patch(f"{MODULE}.execute", new_callable=AsyncMock, side_effect=Exception("DB error")):
        repo = CleanupRepository()
        count = await repo.deactivate_expired_markets()

    assert count == 0


@pytest.mark.anyio
async def test_clear_raw_for_closed_markets():
    """clear_raw_for_closed_markets обнуляет raw у закрытых рынков (по факту закрытия, не по флагу resolved)."""
    with patch(f"{MODULE}.execute", new_callable=AsyncMock, return_value="UPDATE 45749") as mock_exec:
        repo = CleanupRepository()
        count = await repo.clear_raw_for_closed_markets()

    assert count == 45749
    sql = mock_exec.call_args[0][0]
    assert "UPDATE markets" in sql
    assert "raw = NULL" in sql
    assert "raw IS NOT NULL" in sql
    # Критерий — закрытость (is_active=false), НЕ хрупкий флаг resolved:
    # рынки без oracle_snapshot никогда не получают resolved=true и копили raw вечно.
    assert "is_active = false" in sql
    assert "resolved = true" not in sql


@pytest.mark.anyio
async def test_clear_raw_for_closed_markets_no_time_criterion():
    """Критерий — ТОЛЬКО закрытость, без привязки к времени (end_date / updated_at).

    end_date у 27K закрытых рынков в будущем (Polymarket закрывает досрочно) →
    `end_date < x` их пропускает. updated_at омолаживается каждым sync (base-upsert
    тянет до 1500 closed events/15мин, пишет updated_at=now() безусловно) → буфер по
    updated_at тоже никогда не сработает. raw — write-only (0 читателей), страховать
    некого → чистим закрытые сразу, без временного гейта.
    """
    with patch(f"{MODULE}.execute", new_callable=AsyncMock, return_value="UPDATE 0") as mock_exec:
        repo = CleanupRepository()
        await repo.clear_raw_for_closed_markets()

    sql = mock_exec.call_args[0][0]
    assert "end_date" not in sql
    assert "updated_at" not in sql
    assert "make_interval" not in sql


@pytest.mark.anyio
async def test_clear_raw_for_closed_markets_error_returns_zero():
    """При ошибке execute → возвращает 0 (не роняет cleanup-джобу)."""
    with patch(f"{MODULE}.execute", new_callable=AsyncMock, side_effect=Exception("DB error")):
        repo = CleanupRepository()
        count = await repo.clear_raw_for_closed_markets()

    assert count == 0


@pytest.mark.anyio
async def test_delete_snapshots_for_resolved_signals_batches():
    """Суммирует удаления по батчам, останавливается когда batch < batch_size."""
    # Первый батч полный (== batch_size) → цикл продолжается; второй неполный → стоп.
    with patch(
        f"{MODULE}.execute",
        new_callable=AsyncMock,
        side_effect=["DELETE 5000", "DELETE 1227"],
    ) as mock_exec:
        repo = CleanupRepository()
        count = await repo.delete_snapshots_for_resolved_signals(days=7, batch_size=5000)

    assert count == 6227
    assert mock_exec.call_count == 2


@pytest.mark.anyio
async def test_delete_snapshots_for_resolved_signals_sql_uses_resolved_at():
    """SQL фильтрует по resolved_at (не captured_at) — чтобы не рвать траектории."""
    with patch(f"{MODULE}.execute", new_callable=AsyncMock, return_value="DELETE 0") as mock_exec:
        repo = CleanupRepository()
        await repo.delete_snapshots_for_resolved_signals(days=7)

    sql = mock_exec.call_args[0][0]
    assert "DELETE FROM detector_signal_price_snapshots" in sql
    assert "ds.resolved = true" in sql
    assert "ds.resolved_at < $1" in sql
    assert "captured_at" not in sql  # критерий по резолву, не по времени снимка
    assert "LIMIT $2" in sql


@pytest.mark.anyio
async def test_delete_snapshots_for_resolved_signals_error_returns_zero():
    """При ошибке execute → возвращает 0 (не роняет cleanup-джобу)."""
    with patch(f"{MODULE}.execute", new_callable=AsyncMock, side_effect=Exception("DB error")):
        repo = CleanupRepository()
        count = await repo.delete_snapshots_for_resolved_signals(days=7)

    assert count == 0


@pytest.mark.anyio
async def test_delete_unreferenced_closed_markets_guards_all_readers():
    """DELETE закрытых рынков защищён NOT EXISTS по каждой таблице-читателю.

    Полный аудит читателей 2026-07-06: удалять можно только рынки, на которые
    не ссылается НИ ОДНА таблица с аналитической ценностью. Каждый guard здесь —
    конкретный читатель, который иначе тихо теряет данные (или падает по FK).
    """
    with patch(f"{MODULE}.execute", new_callable=AsyncMock, return_value="DELETE 0") as mock_exec:
        repo = CleanupRepository()
        await repo.delete_unreferenced_closed_markets(grace_days=7)

    sql = mock_exec.call_args[0][0]
    assert "DELETE FROM markets" in sql
    assert "is_active = false" in sql
    # Критерий холодности — updated_at (НЕ end_date: у 27K закрытых рынков
    # end_date в будущем, Polymarket закрывает досрочно — они бы жили вечно).
    assert "updated_at < $1" in sql
    assert "end_date" not in sql
    # Guard'ы по text-ключам (polymarket_id / condition_id):
    for table in (
        "detector_signals",  # ретро-резолв + directional audit
        "execution_orders",  # P&L история ставок
        "pipeline_v2_shadow_positions",  # /v2/gate-status distinct_events
        "standalone_v2_predictions",  # STANDALONE_V2 трек-рекорд
        "daily_price_snapshots",  # корпус mv_market_calibration
    ):
        assert f"FROM {table}" in sql, f"нет guard'а по {table}"
    # Guard'ы по uuid (markets.id):
    for table in (
        "transactions",  # whale W/L и PnL (LEFT JOIN markets + token_outcomes)
        "oracle_snapshots",  # CASCADE убил бы mv_market_resolved_outcome
        "market_comments",  # FK БЕЗ CASCADE — голый DELETE упадёт
    ):
        assert f"FROM {table}" in sql, f"нет guard'а по {table}"
    assert sql.count("NOT EXISTS") >= 8
    assert "LIMIT $2" in sql


@pytest.mark.anyio
async def test_delete_unreferenced_closed_markets_batches_and_timeout():
    """Батчинг: суммирует удаления, останавливается при batch < batch_size.
    Каждый батч — с явным timeout (пуловые 30s анти-джойны могут не пережить)."""
    with patch(
        f"{MODULE}.execute",
        new_callable=AsyncMock,
        side_effect=["DELETE 10000", "DELETE 3771"],
    ) as mock_exec:
        repo = CleanupRepository()
        count = await repo.delete_unreferenced_closed_markets(grace_days=7, batch_size=10000)

    assert count == 13771
    assert mock_exec.call_count == 2
    assert mock_exec.call_args.kwargs.get("timeout"), "нет явного timeout — батч умрёт на пуловых 30s"


@pytest.mark.anyio
async def test_delete_unreferenced_closed_markets_error_returns_zero():
    """При ошибке execute → возвращает 0 (не роняет cleanup-джобу)."""
    with patch(f"{MODULE}.execute", new_callable=AsyncMock, side_effect=Exception("DB error")):
        repo = CleanupRepository()
        count = await repo.delete_unreferenced_closed_markets()

    assert count == 0
