"""
Cleanup Repository

Handles bulk deletion of old records to maintain database size.

asyncpg-based, uses global connection pool from app.services.database.
"""

from datetime import UTC, datetime, timedelta
from typing import Any

import structlog

from app.services.database import execute

logger = structlog.get_logger()


class CleanupRepository:
    def __init__(self) -> None:
        pass

    async def delete_old_transactions(
        self, days: int, max_amount: float | None = None, is_whale: bool | None = None, batch_size: int = 5000
    ) -> int:
        """Delete old transactions based on age and optional filters. Батчинг по batch_size — избегаем долгих locks."""
        cutoff = datetime.now(UTC) - timedelta(days=days)

        conditions = ["created_at < $1"]
        params: list[Any] = [cutoff]
        idx = 2

        if max_amount is not None:
            conditions.append(f"amount < ${idx}")
            params.append(max_amount)
            idx += 1

        if is_whale is not None:
            conditions.append(f"is_whale = ${idx}")
            params.append(is_whale)
            idx += 1

        where_clause = " AND ".join(conditions)
        limit_placeholder = f"${idx}"
        params.append(batch_size)

        total = 0
        try:
            while True:
                result = await execute(
                    f"""
                    DELETE FROM transactions
                    WHERE id IN (
                        SELECT id FROM transactions WHERE {where_clause} LIMIT {limit_placeholder}
                    )
                    """,
                    *params,
                )
                batch = int(result.split()[-1])
                total += batch
                if batch < batch_size:
                    break
            if total > 0:
                logger.info("deleted_old_transactions", count=total, days=days)
            return total
        except Exception as e:
            logger.error("failed_to_delete_old_transactions", error=str(e))
            return 0

    async def delete_stale_pending_snapshots(self, days: int = 14) -> int:
        """Удалить pending oracle_snapshots старше N дней (resolved хранятся для calibration)."""
        cutoff = datetime.now(UTC) - timedelta(days=days)
        try:
            result = await execute(
                """
                DELETE FROM oracle_snapshots
                WHERE prediction_correct IS NULL
                  AND outcome = 'pending'
                  AND created_at < $1
                """,
                cutoff,
            )
            count = int(result.split()[-1])
            if count > 0:
                logger.info("deleted_stale_pending_snapshots", count=count, days=days)
            return count
        except Exception as e:
            logger.error("failed_to_delete_stale_pending_snapshots", error=str(e))
            return 0

    async def downsample_resolved_snapshots(self, keep_per_market: int = 5) -> int:
        """Downsampling resolved снапшотов: оставить keep_per_market ключевых точек на рынок.

        Стратегия: первый, Q1, медиана, Q3, финальный (по created_at).
        Это сохраняет:
        - Первый снапшот (snapshot_index=1 для entry signal в backtest)
        - Финальный снапшот (brier_score, prediction_correct)
        - 3 промежуточных (дрейф oracle_score по горизонту)

        При keep_per_market рынков с <=keep_per_market снапшотами — ничего не удаляется.
        """
        try:
            result = await execute(
                """
                WITH market_counts AS (
                    SELECT market_id, COUNT(*) AS cnt
                    FROM oracle_snapshots
                    WHERE outcome IN ('yes', 'no')
                    GROUP BY market_id
                    HAVING COUNT(*) > $1
                ),
                ranked AS (
                    SELECT os.id,
                           os.market_id,
                           ROW_NUMBER() OVER (PARTITION BY os.market_id ORDER BY os.created_at) AS rn,
                           COUNT(*) OVER (PARTITION BY os.market_id) AS total
                    FROM oracle_snapshots os
                    INNER JOIN market_counts mc ON mc.market_id = os.market_id
                    WHERE os.outcome IN ('yes', 'no')
                ),
                keep_ids AS (
                    SELECT id FROM ranked
                    WHERE rn = 1              -- первый (entry signal)
                       OR rn = total          -- финальный (brier_score)
                       OR rn = total / 4      -- Q1
                       OR rn = total / 2      -- медиана
                       OR rn = 3 * total / 4  -- Q3
                )
                DELETE FROM oracle_snapshots
                WHERE outcome IN ('yes', 'no')
                  AND market_id IN (SELECT market_id FROM market_counts)
                  AND id NOT IN (SELECT id FROM keep_ids)
                """,
                keep_per_market,
            )
            count = int(result.split()[-1]) if result else 0
            if count > 0:
                logger.info("downsampled_resolved_snapshots", deleted=count, keep_per_market=keep_per_market)
            return count
        except Exception:
            logger.exception("downsample_resolved_snapshots_failed")
            return 0

    async def delete_inactive_market_analyses(self) -> int:
        """Удалить market_analyses для неактивных рынков."""
        try:
            result = await execute(
                """
                DELETE FROM market_analyses
                WHERE market_id IN (SELECT id FROM markets WHERE is_active = false)
                """,
            )
            count = int(result.split()[-1])
            if count > 0:
                logger.info("deleted_inactive_market_analyses", count=count)
            return count
        except Exception as e:
            logger.error("failed_to_delete_inactive_market_analyses", error=str(e))
            return 0

    async def delete_snapshots_for_resolved_signals(self, days: int = 7, batch_size: int = 5000) -> int:
        """Удалить price-снапшоты сигналов, резолвнутых > N дней назад. Батчинг.

        detector_signal_price_snapshots растёт без retention: съёмка прекращается
        на резолве (resolved=false в writer), но старые снапшоты остаются навсегда.
        На проде (2026-06-14) 9.3M/14.8M строк (63%, ~1.8 GB) — мёртвый груз
        resolved-сигналов. FK ON DELETE CASCADE чистит только при удалении самого
        сигнала, а сигналы не удаляются.

        Критерий — resolved_at, НЕ captured_at: удаляем ВСЮ траекторию сигнала,
        резолвнутого >N дней назад. Порог по captured_at порвал бы траектории
        long-horizon сигналов (D44 до 7д: начало пути старше N дней, хвост — нет),
        сломав signal_decay_study.py, который читает price path от generated_at до
        резолва. По resolved_at недавние резолвы сохраняются целиком.
        """
        cutoff = datetime.now(UTC) - timedelta(days=days)
        total = 0
        try:
            while True:
                result = await execute(
                    """
                    DELETE FROM detector_signal_price_snapshots
                    WHERE ctid IN (
                        SELECT s.ctid
                        FROM detector_signal_price_snapshots s
                        JOIN detector_signals ds ON ds.id = s.detector_signal_id
                        WHERE ds.resolved = true
                          AND ds.resolved_at < $1
                        LIMIT $2
                    )
                    """,
                    cutoff,
                    batch_size,
                )
                batch = int(result.split()[-1])
                total += batch
                if batch < batch_size:
                    break
            if total > 0:
                logger.info("deleted_snapshots_for_resolved_signals", count=total, days=days)
            return total
        except Exception as e:
            logger.error("failed_to_delete_snapshots_for_resolved_signals", error=str(e))
            return 0

    async def delete_stale_price_staleness_logs(self, days: int = 30) -> int:
        """Удалить price_staleness_log старше N дней (телеметрия Плана 1 — гейт + пост-анализ, 30 дней хватает)."""
        cutoff = datetime.now(UTC) - timedelta(days=days)
        try:
            result = await execute(
                "DELETE FROM price_staleness_log WHERE logged_at < $1",
                cutoff,
            )
            count = int(result.split()[-1])
            if count > 0:
                logger.info("deleted_stale_price_staleness_logs", count=count, days=days)
            return count
        except Exception as e:
            logger.error("failed_to_delete_stale_price_staleness_logs", error=str(e))
            return 0

    async def delete_unreferenced_closed_markets(self, grace_days: int = 7, batch_size: int = 10000) -> int:
        """Удалить закрытые рынки, на которые не ссылается ни одна таблица-читатель.

        markets растёт +55-66K строк/нед (firehose короткоживущих рынков Polymarket:
        hourly crypto, спорт, погода) и без row-retention монотонна: строки только
        деактивируются (is_active=false) и теряют raw. Аудит 2026-07-06: ~2/3 таблицы —
        закрытые рынки без единой внешней ссылки (система их синканула и больше
        никогда не трогала). Бэктест ходит в canonical parquet, БД markets не читает.

        Критерий холодности — updated_at, НЕ end_date: у ~27K закрытых рынков end_date
        в будущем (Polymarket закрывает досрочно), по end_date они бы жили вечно.
        updated_at замерзает, как только рынок выпадает из sweep-окна market_sync
        (top-1500 closed events по endDate) — 7 тихих дней = вне всех sweep'ов.

        Каждый NOT EXISTS — конкретный читатель из аудита читателей:
        - detector_signals: ретро-резолв и directional audit (JOIN по polymarket_id);
        - execution_orders / standalone_v2_predictions / pipeline_v2_shadow_positions:
          P&L история и promote-гейты (/v2/gate-status считает distinct_events);
        - daily_price_snapshots: корпус mv_market_calibration (365д окно);
        - transactions: whale W/L и PnL (LEFT JOIN markets ради token_outcomes);
        - oracle_snapshots: FK CASCADE снёс бы их → mv_market_resolved_outcome
          потеряла бы исходы для whale-аналитики;
        - market_comments: FK БЕЗ CASCADE — без guard'а DELETE падает.
        Кэши без исторической ценности (maker_direction_cache, market_signals,
        market_analyses) сознательно НЕ guard'ы — их снесёт FK CASCADE.

        Явный timeout на батч: анти-джойны по 10 таблицам в пуловые 30s могут
        не уложиться (та же причина, что у vacuum_maintenance).
        """
        cutoff = datetime.now(UTC) - timedelta(days=grace_days)
        total = 0
        try:
            while True:
                result = await execute(
                    """
                    DELETE FROM markets
                    WHERE id IN (
                        SELECT m.id
                        FROM markets m
                        WHERE m.is_active = false
                          AND m.updated_at < $1
                          -- text-guard'ы: по polymarket_id И по condition_id ОТДЕЛЬНЫМИ
                          -- NOT EXISTS (сейчас ключи всегда равны, но страхуем расхождение).
                          -- НЕ объединять в OR: OR ломает hash anti-join, planner уходит
                          -- в nested loop без leading-индекса на market_id → таймаут.
                          AND NOT EXISTS (SELECT 1 FROM detector_signals ds WHERE ds.market_id = m.polymarket_id)
                          AND NOT EXISTS (SELECT 1 FROM detector_signals ds2 WHERE ds2.market_id = m.condition_id)
                          AND NOT EXISTS (SELECT 1 FROM execution_orders eo WHERE eo.market_id = m.polymarket_id)
                          AND NOT EXISTS (SELECT 1 FROM execution_orders eo2 WHERE eo2.market_id = m.condition_id)
                          AND NOT EXISTS (
                              SELECT 1 FROM pipeline_v2_shadow_positions sp WHERE sp.market_id = m.polymarket_id
                          )
                          AND NOT EXISTS (
                              SELECT 1 FROM pipeline_v2_shadow_positions sp2 WHERE sp2.market_id = m.condition_id
                          )
                          AND NOT EXISTS (
                              SELECT 1 FROM standalone_v2_predictions svp WHERE svp.market_id = m.polymarket_id
                          )
                          AND NOT EXISTS (
                              SELECT 1 FROM standalone_v2_predictions svp2 WHERE svp2.condition_id = m.condition_id
                          )
                          AND NOT EXISTS (
                              SELECT 1 FROM daily_price_snapshots dps
                              WHERE dps.condition_id = m.condition_id
                          )
                          AND NOT EXISTS (SELECT 1 FROM transactions t WHERE t.market_id = m.id)
                          AND NOT EXISTS (SELECT 1 FROM oracle_snapshots os WHERE os.market_id = m.id)
                          AND NOT EXISTS (SELECT 1 FROM market_comments mc WHERE mc.market_id = m.id)
                        LIMIT $2
                    )
                    """,
                    cutoff,
                    batch_size,
                    timeout=120.0,
                )
                batch = int(result.split()[-1])
                total += batch
                if batch < batch_size:
                    break
            if total > 0:
                logger.info("deleted_unreferenced_closed_markets", count=total, grace_days=grace_days)
            return total
        except Exception as e:
            logger.error("failed_to_delete_unreferenced_closed_markets", error=str(e))
            return total

    async def deactivate_expired_markets(self) -> int:
        """Деактивировать рынки с истёкшим end_date."""
        now = datetime.now(UTC)

        try:
            result = await execute(
                "UPDATE markets SET is_active = false WHERE is_active = true AND end_date < $1",
                now,
            )
            count = int(result.split()[-1])
            if count > 0:
                logger.info("deactivated_expired_markets", count=count)
            return count
        except Exception as e:
            logger.error("failed_to_deactivate_expired_markets", error=str(e))
            return 0

    async def clear_raw_for_closed_markets(self) -> int:
        """Обнулить markets.raw у ЗАКРЫТЫХ рынков (по факту закрытия, не по флагу resolved).

        raw = сырой Gamma API-дамп (safety-net при sync), ~3-4KB/строка в TOAST.
        write-only: не читается НИГДЕ в коде после закрытия (market_sync только пишет;
        полезные поля разложены по колонкам markets). Без обнуления TOAST растёт
        линейно → DB Size Alert по markets (инцидент 2026-06-10: 1041 MB; рецидивы
        2026-06-17: 1066 MB; 2026-06-20: 1052 MB).

        Критерий — ТОЛЬКО закрытость (`is_active = false`). Без временного буфера:
        - НЕ `resolved = true` (рецидив 2026-06-17): флаг выставляется лишь рынкам с
          oracle_snapshot (resolve-sweep делают INNER JOIN oracle_snapshots), рынки без
          снапшота зависают в resolved=false и копят raw вечно.
        - НЕ `end_date` (рецидив 2026-06-20): у 27K закрытых рынков end_date в будущем
          (Polymarket помечает closed=true досрочно) → `end_date < x` их не ловит.
        - НЕ `updated_at`-буфер: base-upsert в market_sync тянет до 1500 closed events
          каждые 15 мин и пишет updated_at=now() безусловно → строка никогда не выходит
          из буфера.
        raw write-only → страховать читателя не нужно, чистим закрытые сразу. Парный
        гейт market_sync (raw=NULL для is_active=false) не даёт raw вернуться между
        прогонами. deactivate_expired_markets выставляет is_active=false до этого шага.
        """
        try:
            result = await execute("UPDATE markets SET raw = NULL WHERE raw IS NOT NULL AND is_active = false")
            count = int(result.split()[-1])
            if count > 0:
                logger.info("cleared_raw_for_closed_markets", count=count)
            return count
        except Exception as e:
            logger.error("failed_to_clear_raw_for_closed_markets", error=str(e))
            return 0
