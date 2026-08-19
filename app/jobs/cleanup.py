"""
Cleanup Job

Orchestrates the deletion of old data across all tables.
Runs as a recurring cron job.
"""

import logging

from app.jobs.trajectory_archive import archive_resolved_trajectories
from app.repositories.cleanup_repository import CleanupRepository

logger = logging.getLogger(__name__)


async def run_storage_cleanup():
    """
    Main entry point for the cleanup job.

    Retention Policy:
    1. Normal transactions (<$1k): 7 days
    2. Whale transactions: хранятся бессрочно (аналитический актив)
    """
    logger.info("Starting storage cleanup job")

    repo = CleanupRepository()

    results = {
        "normal_tx_deleted": 0,
        "markets_deactivated": 0,
        "stale_snapshots_deleted": 0,
        "inactive_analyses_deleted": 0,
        "snapshots_downsampled": 0,
        "markets_raw_cleared": 0,
        "signal_price_snapshots_deleted": 0,
        "price_staleness_logs_deleted": 0,
        "trajectory_rows_archived": 0,
        "unreferenced_markets_deleted": 0,
    }

    try:
        # 1. Деактивировать просроченные рынки (end_date < now)
        results["markets_deactivated"] = await repo.deactivate_expired_markets()

        # 1b. Обнулить raw у ЗАКРЫТЫХ рынков (TOAST retention — иначе markets растёт
        # линейно, DB Size Alert). Критерий — is_active=false (не флаг resolved):
        # рынки без oracle_snapshot никогда не получают resolved=true и копили raw
        # навсегда. Шаг 1 выше уже деактивировал просроченные → ловятся здесь.
        results["markets_raw_cleared"] = await repo.clear_raw_for_closed_markets()

        # 2. Cleanup normal transactions (small fish)
        # amount < 1000, older than 7 days
        results["normal_tx_deleted"] = await repo.delete_old_transactions(days=7, max_amount=1000, is_whale=False)

        # 3. Whale transactions — НЕ удаляем.
        # Нужны для: whale_copy simulation, W/L пересчёт, timing scores,
        # oracle_calculator (последние 20 tx/маркет), PnL по незакрытым маркетам.
        # Рост ~550 MB/год — не проблема.

        # 4. Retention: pending oracle_snapshots > 14 дней (resolved хранятся для calibration)
        results["stale_snapshots_deleted"] = await repo.delete_stale_pending_snapshots(days=14)

        # 6. Retention: market_analyses для неактивных рынков
        results["inactive_analyses_deleted"] = await repo.delete_inactive_market_analyses()

        # 8. Downsample resolved snapshots: оставить 24 точки на рынок (≈1/час).
        # Было 5 — недостаточно для calibration_grid_search и backtest.
        # 24 = достаточно для часовой гранулярности + управляемый рост (~2.5 GB/мес).
        results["snapshots_downsampled"] = await repo.downsample_resolved_snapshots(keep_per_market=24)

        # 9 (удалён 2026-07-06): retail_trades переименована в _deprecated_...
        # миграцией 20260505095006, шаг ежедневно логировал relation does not exist.

        # 10. Retention: price-снапшоты сигналов, резолвнутых >7 дней назад.
        # detector_signal_price_snapshots копился без retention (2886 MB, 63% —
        # мёртвый груз resolved-сигналов). По resolved_at, не captured_at — иначе
        # рвутся траектории long-horizon сигналов для signal_decay_study.py.
        # ПЕРЕД удалением — архив в parquet (research/data/trajectory_archive/):
        # это единственный источник полных траекторий для exit-policy ресёрча.
        # Архив упал → удаление пропускаем, данные важнее диска (догоним завтра).
        # try/except: archive должен возвращать ok=False сам, но необработанное
        # исключение (инцидент 2026-07-06: PermissionError на mkdir) не должно
        # убивать шаги 11-12 — они от архива не зависят.
        try:
            archive = await archive_resolved_trajectories(days=7)
        except Exception as e:
            logger.error(f"Trajectory archive raised, skipping snapshot deletion: {e}")
            archive = {"ok": False, "rows": f"failed: {e}"}
        results["trajectory_rows_archived"] = archive["rows"]
        if archive["ok"]:
            results["signal_price_snapshots_deleted"] = await repo.delete_snapshots_for_resolved_signals(days=7)
        else:
            results["signal_price_snapshots_deleted"] = "skipped_archive_failed"

        # 11. Retention: price_staleness_log (телеметрия Плана 1, docs/superpowers/plans/
        # 2026-07-03-event-driven-price-pipeline.md) — 30 дней достаточно для гейта + пост-анализа.
        results["price_staleness_logs_deleted"] = await repo.delete_stale_price_staleness_logs(days=30)

        # 12. Retention: закрытые рынки без единой внешней ссылки, холодные >7 дней.
        # markets растёт +55-66K строк/нед (firehose короткоживущих рынков Polymarket),
        # ~2/3 таблицы — рынки, которые система синканула и больше никогда не трогала
        # (аудит читателей 2026-07-06). Guard'ы по всем таблицам с исторической
        # ценностью — см. docstring delete_unreferenced_closed_markets.
        results["unreferenced_markets_deleted"] = await repo.delete_unreferenced_closed_markets(grace_days=7)

        logger.info(f"Cleanup completed successfully: {results}")
        return results

    except Exception as e:
        logger.error(f"Cleanup job failed: {e}")
        return {"error": str(e), "results_so_far": results}
