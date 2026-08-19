"""Unit тесты для app.jobs.trajectory_archive — архивация траекторий перед удалением."""
from __future__ import annotations

import uuid
from datetime import UTC, datetime
from decimal import Decimal
from unittest.mock import AsyncMock, patch

import pyarrow.parquet as pq
import pytest

MODULE = "app.jobs.trajectory_archive"


def _row(sig: uuid.UUID, ts: datetime, price: str) -> dict:
    return {"detector_signal_id": sig, "captured_at": ts, "yes_price": Decimal(price)}


@pytest.mark.asyncio
async def test_archives_rows_to_parquet(tmp_path):
    """Строки уходящих траекторий пишутся в parquet: uuid→str, price→float."""
    from app.jobs.trajectory_archive import archive_resolved_trajectories

    sig = uuid.uuid4()
    ts = datetime(2026, 6, 20, 12, 0, tzinfo=UTC)
    with (
        patch(f"{MODULE}.fetch", new_callable=AsyncMock,
              side_effect=[[{"id": sig}], [_row(sig, ts, "0.42")], []]),
        patch(f"{MODULE}.ARCHIVE_DIR", tmp_path),
    ):
        result = await archive_resolved_trajectories(days=7, run_date="2026-07-06")

    assert result["ok"] is True
    assert result["rows"] == 1
    table = pq.read_table(tmp_path / "snapshots_2026-07-06.parquet")
    assert table.num_rows == 1
    assert table.column("detector_signal_id")[0].as_py() == str(sig)
    assert table.column("yes_price")[0].as_py() == pytest.approx(0.42)


@pytest.mark.asyncio
async def test_zero_rows_keeps_existing_file(tmp_path):
    """Повторный запуск в тот же день после удаления (0 строк) не затирает готовый файл."""
    from app.jobs.trajectory_archive import archive_resolved_trajectories

    existing = tmp_path / "snapshots_2026-07-06.parquet"
    existing.write_bytes(b"not-empty")
    with (
        patch(f"{MODULE}.fetch", new_callable=AsyncMock, return_value=[]),
        patch(f"{MODULE}.ARCHIVE_DIR", tmp_path),
    ):
        result = await archive_resolved_trajectories(days=7, run_date="2026-07-06")

    assert result["ok"] is True
    assert result["rows"] == 0
    assert existing.read_bytes() == b"not-empty"


@pytest.mark.asyncio
async def test_db_error_returns_not_ok(tmp_path):
    """Ошибка БД → ok=False, никакого файла (сигнал cleanup'у пропустить удаление)."""
    from app.jobs.trajectory_archive import archive_resolved_trajectories

    with (
        patch(f"{MODULE}.fetch", new_callable=AsyncMock, side_effect=RuntimeError("db down")),
        patch(f"{MODULE}.ARCHIVE_DIR", tmp_path),
    ):
        result = await archive_resolved_trajectories(days=7, run_date="2026-07-06")

    assert result["ok"] is False
    assert list(tmp_path.iterdir()) == []


@pytest.mark.asyncio
async def test_mid_write_failure_cleans_up_tmp(tmp_path):
    """Сбой на середине записи (второй fetch упал) → ok=False, ни .parquet, ни .tmp не остаётся."""
    from app.jobs.trajectory_archive import archive_resolved_trajectories

    sig = uuid.uuid4()
    with (
        patch(
            f"{MODULE}.fetch",
            new_callable=AsyncMock,
            side_effect=[[{"id": sig}], RuntimeError("disk/db fail")],
        ),
        patch(f"{MODULE}.ARCHIVE_DIR", tmp_path),
    ):
        result = await archive_resolved_trajectories(days=7, run_date="2026-07-06")

    assert result["ok"] is False
    remaining = list(tmp_path.iterdir())
    assert not any(p.suffix == ".parquet" for p in remaining)
    assert not any(p.name.endswith(".tmp") for p in remaining)


@pytest.mark.asyncio
async def test_mkdir_failure_returns_not_ok(tmp_path):
    """ARCHIVE_DIR.mkdir упал (PermissionError на root-owned mount, инцидент
    2026-07-06) → ok=False, исключение НЕ пролетает наружу."""
    from app.jobs.trajectory_archive import archive_resolved_trajectories

    blocker = tmp_path / "not_a_dir"
    blocker.write_bytes(b"")  # файл на месте каталога → mkdir кидает FileExistsError
    with patch(f"{MODULE}.ARCHIVE_DIR", blocker):
        result = await archive_resolved_trajectories(days=7, run_date="2026-07-06")

    assert result["ok"] is False


@pytest.mark.asyncio
async def test_cleanup_skips_delete_when_archive_fails():
    """Архивация упала → удаление снапшотов пропускается; успех → удаление идёт."""
    from app.jobs.cleanup import run_storage_cleanup

    with (
        patch("app.jobs.cleanup.CleanupRepository") as repo_cls,
        patch(
            "app.jobs.cleanup.archive_resolved_trajectories",
            new_callable=AsyncMock,
            return_value={"ok": False, "rows": 0, "file": None},
        ),
    ):
        repo = repo_cls.return_value
        for m in (
            "deactivate_expired_markets", "clear_raw_for_closed_markets",
            "delete_old_transactions", "delete_stale_pending_snapshots",
            "delete_inactive_market_analyses", "downsample_resolved_snapshots",
            "delete_snapshots_for_resolved_signals",
            "delete_stale_price_staleness_logs", "delete_unreferenced_closed_markets",
        ):
            setattr(repo, m, AsyncMock(return_value=0))
        results = await run_storage_cleanup()

    repo.delete_snapshots_for_resolved_signals.assert_not_awaited()
    assert results["signal_price_snapshots_deleted"] == "skipped_archive_failed"


@pytest.mark.asyncio
async def test_cleanup_deletes_after_successful_archive():
    """Успешный архив → удаление вызывается с теми же days=7, rows попадают в results."""
    from app.jobs.cleanup import run_storage_cleanup

    with (
        patch("app.jobs.cleanup.CleanupRepository") as repo_cls,
        patch(
            "app.jobs.cleanup.archive_resolved_trajectories",
            new_callable=AsyncMock,
            return_value={"ok": True, "rows": 42, "file": "snapshots_2026-07-06.parquet"},
        ),
    ):
        repo = repo_cls.return_value
        for m in (
            "deactivate_expired_markets", "clear_raw_for_closed_markets",
            "delete_old_transactions", "delete_stale_pending_snapshots",
            "delete_inactive_market_analyses", "downsample_resolved_snapshots",
            "delete_snapshots_for_resolved_signals",
            "delete_stale_price_staleness_logs", "delete_unreferenced_closed_markets",
        ):
            setattr(repo, m, AsyncMock(return_value=0))
        results = await run_storage_cleanup()

    repo.delete_snapshots_for_resolved_signals.assert_awaited_once_with(days=7)
    assert results["trajectory_rows_archived"] == 42


@pytest.mark.asyncio
async def test_cleanup_continues_when_archive_raises():
    """Необработанное исключение из архива (инцидент 2026-07-06: PermissionError)
    не убивает run: снапшоты пропущены, шаги 11-12 выполняются."""
    from app.jobs.cleanup import run_storage_cleanup

    with (
        patch("app.jobs.cleanup.CleanupRepository") as repo_cls,
        patch(
            "app.jobs.cleanup.archive_resolved_trajectories",
            new_callable=AsyncMock,
            side_effect=PermissionError("[Errno 13] research/data/trajectory_archive"),
        ),
    ):
        repo = repo_cls.return_value
        for m in (
            "deactivate_expired_markets", "clear_raw_for_closed_markets",
            "delete_old_transactions", "delete_stale_pending_snapshots",
            "delete_inactive_market_analyses", "downsample_resolved_snapshots",
            "delete_snapshots_for_resolved_signals",
            "delete_stale_price_staleness_logs", "delete_unreferenced_closed_markets",
        ):
            setattr(repo, m, AsyncMock(return_value=0))
        results = await run_storage_cleanup()

    repo.delete_snapshots_for_resolved_signals.assert_not_awaited()
    repo.delete_stale_price_staleness_logs.assert_awaited_once()
    repo.delete_unreferenced_closed_markets.assert_awaited_once()
    assert results["signal_price_snapshots_deleted"] == "skipped_archive_failed"
    assert str(results["trajectory_rows_archived"]).startswith("failed:")
    assert "error" not in results
