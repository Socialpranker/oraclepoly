"""
Oracle Snapshot Repository — data access layer for oracle_snapshots table.

asyncpg-based, uses global connection pool from app.services.database.
"""

from datetime import UTC, datetime
from typing import Any

from app.repositories.base import BaseRepository
from app.services.database import execute, fetch, fetchrow, fetchval, records_to_list


class OracleSnapshotRepository(BaseRepository):
    def __init__(self) -> None:
        pass

    async def create(self, snapshot_data: dict[str, Any]) -> dict[str, Any]:
        """Создать новый snapshot."""
        return await self._create("oracle_snapshots", snapshot_data)

    async def resolve_for_market(self, market_id: str, outcome: str, final_price: float) -> int:
        """Resolve pending snapshots для рынка: вычислить brier_score и prediction_correct.

        Один UPDATE вместо N — brier_score и prediction_correct вычисляются в SQL.
        """
        outcome_binary = 1.0 if outcome == "yes" else 0.0
        now = datetime.now(UTC)

        result = await execute(
            """
            UPDATE oracle_snapshots
            SET outcome = $1,
                final_price = $2,
                brier_score = ROUND(POWER(oracle_score - $3, 2), 4),
                prediction_correct = (oracle_score >= 0.5) = ($3 = 1.0),
                resolved_at = $4
            WHERE market_id = $5 AND outcome = 'pending'
            """,
            outcome,
            final_price,
            outcome_binary,
            now,
            market_id,
        )
        # execute возвращает 'UPDATE N'
        count = int(result.split()[-1]) if result else 0
        return count

    async def get_summary_stats(self, period_days: int | None = None) -> dict[str, Any]:
        """Сводная статистика по resolved snapshots. period_days фильтрует по resolved_at.

        SQL aggregation — не грузим все строки в Python.
        """
        base = "WHERE outcome != $1"
        params: list[Any] = ["pending"]
        if period_days is not None:
            base += " AND resolved_at >= NOW() - INTERVAL '1 day' * $2"
            params.append(period_days)

        row = await fetchrow(
            f"SELECT COUNT(*) AS total_resolved, "
            f"COUNT(*) FILTER (WHERE prediction_correct = true) AS correct, "
            f"COALESCE(AVG(brier_score), 0) AS avg_brier "
            f"FROM oracle_snapshots {base}",
            *params,
        )
        total_resolved = row["total_resolved"]
        if total_resolved == 0:
            return {"total_resolved": 0, "accuracy": 0.0, "avg_brier_score": 0.0}

        correct = row["correct"]
        avg_brier = float(row["avg_brier"])

        return {
            "total_resolved": total_resolved,
            "accuracy": round(correct / total_resolved, 4),
            "avg_brier_score": round(avg_brier, 4),
        }

    async def get_time_to_resolve_stats(self, period_days: int | None = None) -> dict[str, Any]:
        """Статистика времени до resolve: avg, median, p75 в часах."""
        base = "WHERE os.outcome != $1 AND os.resolved_at IS NOT NULL"
        params: list[Any] = ["pending"]
        if period_days is not None:
            base += " AND os.resolved_at >= NOW() - INTERVAL '1 day' * $2"
            params.append(period_days)

        row = await fetchrow(
            f"""
            SELECT
                COUNT(*) AS total,
                COALESCE(AVG(EXTRACT(EPOCH FROM (os.resolved_at - os.created_at)) / 3600), 0) AS avg_hours,
                COALESCE(percentile_cont(0.5) WITHIN GROUP (
                    ORDER BY EXTRACT(EPOCH FROM (os.resolved_at - os.created_at)) / 3600
                ), 0) AS median_hours,
                COALESCE(percentile_cont(0.75) WITHIN GROUP (
                    ORDER BY EXTRACT(EPOCH FROM (os.resolved_at - os.created_at)) / 3600
                ), 0) AS p75_hours
            FROM oracle_snapshots os
            {base}
            """,
            *params,
        )
        return {
            "total": row["total"],
            "avg_hours": round(float(row["avg_hours"]), 2),
            "median_hours": round(float(row["median_hours"]), 2),
            "p75_hours": round(float(row["p75_hours"]), 2),
        }

    async def get_ttr_breakdown(self, period_days: int | None = None) -> dict[str, Any]:
        """Breakdown времени до resolve: бакеты по длительности + категории."""
        base = "WHERE os.outcome != $1 AND os.resolved_at IS NOT NULL"
        params: list[Any] = ["pending"]
        if period_days is not None:
            base += " AND os.resolved_at >= NOW() - INTERVAL '1 day' * $2"
            params.append(period_days)

        # Бакеты по длительности
        buckets_rows = await fetch(
            f"""
            SELECT
                CASE
                    WHEN EXTRACT(EPOCH FROM (os.resolved_at - os.created_at)) / 3600 < 6 THEN '0-6h'
                    WHEN EXTRACT(EPOCH FROM (os.resolved_at - os.created_at)) / 3600 < 24 THEN '6-24h'
                    WHEN EXTRACT(EPOCH FROM (os.resolved_at - os.created_at)) / 3600 < 72 THEN '1-3d'
                    WHEN EXTRACT(EPOCH FROM (os.resolved_at - os.created_at)) / 3600 < 168 THEN '3-7d'
                    ELSE '7d+'
                END AS bucket,
                COUNT(*) AS total,
                COUNT(*) FILTER (WHERE os.prediction_correct = true) AS correct,
                ROUND(AVG(os.brier_score)::numeric, 4) AS avg_brier,
                ROUND(AVG(EXTRACT(EPOCH FROM (os.resolved_at - os.created_at)) / 3600)::numeric, 2) AS avg_hours
            FROM oracle_snapshots os
            {base}
            GROUP BY bucket
            ORDER BY MIN(EXTRACT(EPOCH FROM (os.resolved_at - os.created_at)))
            """,
            *params,
        )
        buckets = []
        for r in buckets_rows:
            total = r["total"]
            correct = r["correct"]
            buckets.append(
                {
                    "bucket": r["bucket"],
                    "total": total,
                    "correct": correct,
                    "accuracy": round(correct / total, 4) if total > 0 else 0.0,
                    "avg_brier": float(r["avg_brier"]) if r["avg_brier"] is not None else 0.0,
                    "avg_hours": float(r["avg_hours"]) if r["avg_hours"] is not None else 0.0,
                }
            )

        # Категории
        categories_rows = await fetch(
            f"""
            SELECT
                COALESCE(os.category, 'Unknown') AS category,
                COUNT(*) AS total,
                COUNT(*) FILTER (WHERE os.prediction_correct = true) AS correct,
                ROUND(AVG(EXTRACT(EPOCH FROM (os.resolved_at - os.created_at)) / 3600)::numeric, 2) AS avg_hours,
                COALESCE(percentile_cont(0.5) WITHIN GROUP (
                    ORDER BY EXTRACT(EPOCH FROM (os.resolved_at - os.created_at)) / 3600
                ), 0) AS median_hours
            FROM oracle_snapshots os
            {base}
            GROUP BY os.category
            ORDER BY COUNT(*) DESC
            """,
            *params,
        )
        categories = []
        for r in categories_rows:
            total = r["total"]
            correct = r["correct"]
            categories.append(
                {
                    "category": r["category"],
                    "total": total,
                    "accuracy": round(correct / total, 4) if total > 0 else 0.0,
                    "avg_hours": float(r["avg_hours"]) if r["avg_hours"] is not None else 0.0,
                    "median_hours": round(float(r["median_hours"]), 2),
                }
            )

        return {"buckets": buckets, "categories": categories}

    async def get_snapshots(
        self,
        outcome: str | None = None,
        market_id: str | None = None,
        limit: int = 100,
        offset: int = 0,
    ) -> list[dict[str, Any]]:
        """Получить snapshots с JOIN на markets (question, category)."""
        conditions: list[str] = []
        params: list[Any] = []
        idx = 1

        if outcome is not None:
            conditions.append(f"os.outcome = ${idx}")
            params.append(outcome)
            idx += 1
        if market_id is not None:
            conditions.append(f"os.market_id = ${idx}")
            params.append(market_id)
            idx += 1

        where_clause = f"WHERE {' AND '.join(conditions)}" if conditions else ""

        query = f"""
            SELECT os.*, m.question AS market_question, COALESCE(os.category, m.category) AS market_category
            FROM oracle_snapshots os
            LEFT JOIN markets m ON m.id = os.market_id
            {where_clause}
            ORDER BY os.created_at DESC
            LIMIT ${idx} OFFSET ${idx + 1}
        """
        params.extend([limit, offset])

        rows = await fetch(query, *params)
        # Формируем вложенный объект markets для обратной совместимости
        result = []
        for r in rows:
            d = dict(r)
            d["markets"] = {
                "question": d.pop("market_question", None),
                "category": d.pop("market_category", None),
            }
            result.append(d)
        return result

    async def get_rolling_brier(self, n: int = 50) -> float:
        """Avg brier_score of last N resolved snapshots.

        Falls back to 0.25 (conservative) if insufficient data.
        """
        val = await fetchval(
            """
            SELECT AVG(brier_score) FROM (
                SELECT brier_score FROM oracle_snapshots
                WHERE brier_score IS NOT NULL
                  AND outcome IS NOT NULL AND outcome != $1
                ORDER BY resolved_at DESC LIMIT $2
            ) sub
            """,
            "pending",
            n,
        )
        if val is None:
            return 0.25
        return float(val)

    async def get_pending_count(self) -> int:
        """Количество pending snapshots."""
        count = await fetchval(
            "SELECT count(*) FROM oracle_snapshots WHERE outcome = $1",
            "pending",
        )
        return count or 0

    async def get_total_count(self) -> int:
        """Общее количество snapshots."""
        count = await fetchval("SELECT count(*) FROM oracle_snapshots")
        return count or 0

    async def get_filtered_count(
        self,
        outcome: str | None = None,
        market_id: str | None = None,
    ) -> int:
        """Количество snapshots с учётом фильтров."""
        conditions: list[str] = []
        params: list[Any] = []
        idx = 1

        if outcome is not None:
            conditions.append(f"outcome = ${idx}")
            params.append(outcome)
            idx += 1
        if market_id is not None:
            conditions.append(f"market_id = ${idx}")
            params.append(market_id)
            idx += 1

        where_clause = f"WHERE {' AND '.join(conditions)}" if conditions else ""
        count = await fetchval(f"SELECT count(*) FROM oracle_snapshots {where_clause}", *params)
        return count or 0

    async def get_calibration_data(self) -> list[dict[str, Any]]:
        """Данные для калибровки: binning oracle_score vs actual outcome.

        SQL-side aggregation с width_bucket — не грузим все строки в Python.
        """
        rows = await fetch(
            """
            SELECT
                LEAST(FLOOR(oracle_score::numeric * 10)::int, 9) AS bin_idx,
                ROUND(AVG(oracle_score), 4) AS predicted,
                ROUND(AVG(CASE WHEN outcome = 'yes' THEN 1.0 ELSE 0.0 END), 4) AS actual,
                COUNT(*) AS count
            FROM oracle_snapshots
            WHERE outcome != $1
            GROUP BY bin_idx
            ORDER BY bin_idx
            """,
            "pending",
        )

        return [
            {
                "bin": f"{row['bin_idx'] * 0.1:.1f}-{row['bin_idx'] * 0.1 + 0.1:.1f}",
                "predicted": float(row["predicted"]),
                "actual": float(row["actual"]),
                "count": row["count"],
            }
            for row in rows
        ]

    async def get_simulation_data(self, max_horizon_days: int | None = None) -> list[dict[str, Any]]:
        """Resolved snapshots с category и end_date для симуляции банка."""
        params: list[Any] = ["pending"]
        idx = 2

        horizon_clause = ""
        if max_horizon_days is not None:
            horizon_clause = f"AND (m.end_date IS NULL OR m.end_date <= NOW() + INTERVAL '1 day' * ${idx})"
            params.append(max_horizon_days)
            idx += 1

        query = f"""
            SELECT
                os.id,
                os.market_id,
                os.oracle_score,
                os.market_price,
                os.outcome,
                os.divergence,
                os.created_at,
                os.resolved_at,
                m.question,
                m.category,
                m.end_date,
                m.event_id,
                COALESCE(os.volume_24h, m.volume_24h, 0) AS volume_24h,
                os.whale_contribution_raw,
                os.news_contribution_raw,
                os.sm_registry_raw,
                os.oi_factor_raw,
                os.concentration_factor_raw,
                os.micro_factor_raw,
                os.sm_fresh_raw,
                os.sm_cluster_raw,
                EXISTS(
                    SELECT 1 FROM transactions t
                    WHERE t.market_id = os.market_id AND t.amount >= 5000
                ) AS has_whale_activity
            FROM oracle_snapshots os
            LEFT JOIN markets m ON m.id = os.market_id
            WHERE os.outcome != $1
            {horizon_clause}
            ORDER BY os.created_at ASC
        """
        rows = await fetch(query, *params)
        return records_to_list(rows)

    async def get_live_signals_data(self) -> list[dict[str, Any]]:
        """Последний pending snapshot на каждый активный рынок для live-сигналов.

        price_avg_7d — proxy VWAP, посчитан как AVG(market_price) за 7 дней
        (HF-research показывает: vwap_7d corr -0.272 с outcome для NO).

        Оптимизация (2026-04-29): фильтр created_at > 25h на pending snapshots
        сокращает выборку с 1.8M до ~240K строк. Рынки без свежего snapshot
        (>25h) исключены — oracle_recalculate их не обновляет, они не live.
        vwap_7d вынесен в CTE (один групповой scan вместо 14K LATERAL subqueries).
        Индекс: idx_oracle_snapshots_pending_recent (created_at DESC, market_id)
        WHERE outcome='pending'. Время выполнения: ~3.5s → ~1.8s.
        """
        query = """
            WITH vwap_7d AS (
                SELECT market_id, AVG(market_price)::numeric(8,4) AS price_avg_7d
                FROM oracle_snapshots
                WHERE created_at > NOW() - INTERVAL '7 days'
                GROUP BY market_id
                HAVING COUNT(*) >= 3
            ),
            latest_pending AS (
                SELECT DISTINCT ON (market_id)
                    market_id, oracle_score, market_price, divergence, created_at,
                    whale_contribution_raw, news_contribution_raw, sm_registry_raw,
                    whale_tx_count, micro_factor_raw, z_posterior,
                    maker_direction_raw, taker_imbalance_raw
                FROM oracle_snapshots
                WHERE outcome = 'pending'
                  AND created_at > NOW() - INTERVAL '25 hours'
                ORDER BY market_id, created_at DESC
            )
            SELECT
                lp.market_id,
                lp.oracle_score,
                lp.market_price,
                lp.divergence,
                lp.created_at,
                lp.whale_contribution_raw,
                lp.news_contribution_raw,
                lp.sm_registry_raw,
                lp.whale_tx_count,
                lp.micro_factor_raw,
                lp.z_posterior,
                lp.maker_direction_raw,
                lp.taker_imbalance_raw,
                m.question,
                m.category,
                m.current_price,
                COALESCE(m.volume_24h, 0) AS volume_24h,
                m.end_date,
                m.event_id,
                v.price_avg_7d
            FROM latest_pending lp
            JOIN markets m ON m.id = lp.market_id AND m.is_active = true
            LEFT JOIN vwap_7d v ON v.market_id = lp.market_id
        """
        rows = await fetch(query)
        return records_to_list(rows)

    async def get_active_markets_for_contra_bias(self) -> list[dict[str, Any]]:
        """Активные рынки для contra-bias pipeline (без oracle_score dependency).

        Возвращает market data + последний whale snapshot + maker_direction + z_posterior.
        Плюс clob_token_ids — нужен для OBI signal source (orderbook fetch per token).
        """
        query = """
            SELECT
                m.id AS market_id,
                m.question,
                m.category,
                m.current_price,
                COALESCE(m.volume_24h, 0) AS volume_24h,
                m.end_date,
                m.event_id,
                m.created_at,
                m.clob_token_ids,
                COALESCE(latest_snap.whale_tx_count, 0) AS whale_tx_count,
                COALESCE(latest_snap.whale_contribution_raw, 0) AS whale_contribution_raw,
                latest_snap.z_posterior,
                latest_snap.maker_direction_raw,
                latest_snap.taker_imbalance_raw,
                vwap_proxy.price_avg_7d
            FROM markets m
            LEFT JOIN LATERAL (
                SELECT os.whale_tx_count, os.whale_contribution_raw,
                       os.z_posterior, os.maker_direction_raw, os.taker_imbalance_raw
                FROM oracle_snapshots os
                WHERE os.market_id = m.id
                ORDER BY os.created_at DESC
                LIMIT 1
            ) latest_snap ON true
            LEFT JOIN LATERAL (
                SELECT AVG(os2.market_price)::numeric(8,4) AS price_avg_7d
                FROM oracle_snapshots os2
                WHERE os2.market_id = m.id
                  AND os2.created_at > NOW() - INTERVAL '7 days'
                HAVING COUNT(*) >= 3
            ) vwap_proxy ON true
            WHERE m.is_active = true
              AND m.current_price IS NOT NULL
              AND m.end_date > NOW()
              AND COALESCE(m.volume_24h, 0) > 0
        """
        rows = await fetch(query)
        return records_to_list(rows)

    async def get_whale_copy_data(
        self,
        min_amount: float = 25000,
        max_horizon_days: int | None = None,
        min_win_rate: float = 0.0,
        min_expert_weight: float = 0.0,
        min_trades: int = 1,
        min_volume_24h: float = 0.0,
        categories: list[str] | None = None,
        exclude_categories: list[str] | None = None,
        resolved_after: str | None = None,
        min_pnl_usd: float = 0.0,
        min_roi_percent: float = 0.0,
        min_resolved_bets: int = 0,
        limit: int = 5000,
    ) -> list[dict[str, Any]]:
        """Whale транзакции + resolved outcomes для copy-симуляции (v2: CTE whale_cat_stats — category-specific W/L)."""
        params: list[Any] = [min_amount]
        idx = 2

        where_parts: list[str] = []

        if max_horizon_days is not None:
            where_parts.append(f"AND (m.end_date IS NULL OR m.end_date <= NOW() + INTERVAL '1 day' * ${idx})")
            params.append(max_horizon_days)
            idx += 1

        if resolved_after is not None:
            where_parts.append(f"AND os.resolved_at >= ${idx}::date")
            params.append(resolved_after)
            idx += 1

        if min_win_rate > 0:
            where_parts.append(
                f"AND CASE WHEN (COALESCE(w.wins, 0) + COALESCE(w.losses, 0)) > 0"
                f" THEN COALESCE(w.wins, 0)::float / (COALESCE(w.wins, 0) + COALESCE(w.losses, 0)) * 100"
                f" ELSE 0 END >= ${idx}"
            )
            params.append(min_win_rate)
            idx += 1

        if min_expert_weight > 0:
            where_parts.append(f"AND COALESCE(w.expert_weight, 0) >= ${idx}")
            params.append(min_expert_weight)
            idx += 1

        if min_trades > 1:
            where_parts.append(f"AND (COALESCE(w.wins, 0) + COALESCE(w.losses, 0)) >= ${idx}")
            params.append(min_trades)
            idx += 1

        if min_volume_24h > 0:
            where_parts.append(f"AND COALESCE(m.volume_24h, 0) >= ${idx}")
            params.append(min_volume_24h)
            idx += 1

        if categories:
            where_parts.append(f"AND m.category = ANY(${idx}::text[])")
            params.append(categories)
            idx += 1

        if exclude_categories:
            where_parts.append(f"AND NOT (m.category = ANY(${idx}::text[]))")
            params.append(exclude_categories)
            idx += 1

        if min_pnl_usd > 0:
            where_parts.append(f"AND COALESCE(w.pnl_usd, 0) >= ${idx}")
            params.append(min_pnl_usd)
            idx += 1

        if min_roi_percent > 0:
            where_parts.append(f"AND COALESCE(w.roi_percent, 0) >= ${idx}")
            params.append(min_roi_percent)
            idx += 1

        if min_resolved_bets > 0:
            where_parts.append(f"AND (COALESCE(w.wins, 0) + COALESCE(w.losses, 0)) >= ${idx}")
            params.append(min_resolved_bets)
            idx += 1

        extra_where = "\n                ".join(where_parts)

        # LIMIT параметр — всегда последний
        limit_placeholder = f"${idx}"
        params.append(limit)

        query = f"""
            WITH market_outcomes AS (
                SELECT market_id, outcome, resolved_at
                FROM mv_market_resolved_outcome
            ),
            whale_cat_stats AS (
                SELECT
                    t2.whale_id,
                    COALESCE(m2.category, 'Unknown') AS category,
                    COUNT(*) FILTER (WHERE
                        (UPPER(t2.outcome) = 'YES' AND mo2.outcome = 'yes')
                        OR (UPPER(t2.outcome) = 'NO' AND mo2.outcome = 'no')
                    ) AS cat_wins,
                    COUNT(*) FILTER (WHERE
                        (UPPER(t2.outcome) = 'YES' AND mo2.outcome = 'no')
                        OR (UPPER(t2.outcome) = 'NO' AND mo2.outcome = 'yes')
                    ) AS cat_losses
                FROM transactions t2
                JOIN market_outcomes mo2 ON mo2.market_id = t2.market_id
                JOIN markets m2 ON m2.id = t2.market_id
                WHERE t2.outcome IS NOT NULL
                GROUP BY t2.whale_id, COALESCE(m2.category, 'Unknown')
            )
            SELECT
                t.market_id,
                t.whale_id,
                w.address AS whale_address,
                COALESCE(w.display_name, LEFT(w.address, 10)) AS whale_name,
                t.outcome AS tx_outcome,
                t.amount,
                t.price,
                mo.outcome AS market_outcome,
                COALESCE(m.category, 'Unknown') AS category,
                COALESCE(m.question, '') AS question,
                t.created_at,
                mo.resolved_at,
                w.whale_tier,
                COALESCE(w.expert_weight, 0) AS expert_weight,
                CASE WHEN (COALESCE(w.wins, 0) + COALESCE(w.losses, 0)) > 0
                     THEN COALESCE(w.wins, 0)::float / (COALESCE(w.wins, 0) + COALESCE(w.losses, 0)) * 100
                     ELSE 0 END AS win_rate,
                (COALESCE(w.wins, 0) + COALESCE(w.losses, 0)) AS total_trades,
                COALESCE(m.volume_24h, 0) AS volume_24h,
                COALESCE(wcs.cat_wins, 0) AS whale_cat_wins,
                COALESCE(wcs.cat_losses, 0) AS whale_cat_losses,
                COALESCE(w.pnl_usd, 0) AS whale_pnl_usd,
                COALESCE(w.roi_percent, 0) AS whale_roi_percent,
                w.whale_style
            FROM transactions t
            JOIN market_outcomes mo ON mo.market_id = t.market_id
            JOIN markets m ON m.id = t.market_id
            LEFT JOIN whales w ON w.id = t.whale_id
            LEFT JOIN whale_cat_stats wcs
                ON wcs.whale_id = t.whale_id
                AND wcs.category = COALESCE(m.category, 'Unknown')
            WHERE t.amount >= $1
                AND t.outcome IS NOT NULL
                AND t.price IS NOT NULL
                AND t.price > 0
                {extra_where}
            ORDER BY t.created_at ASC
            LIMIT {limit_placeholder}
        """
        rows = await fetch(query, *params)
        return records_to_list(rows)

    async def export_csv_data(self, limit: int = 50000, days: int | None = None) -> list[dict[str, Any]]:
        """Экспорт resolved snapshots с категорией рынка."""
        params: list[Any] = ["pending"]
        date_filter = ""
        if days is not None:
            date_filter = "AND os.created_at >= NOW() - INTERVAL '1 day' * $2"
            params.append(days)
        limit_param = f"${len(params) + 1}"
        params.append(limit)

        rows = await fetch(
            f"""
            SELECT os.*, m.category AS market_category
            FROM oracle_snapshots os
            LEFT JOIN markets m ON m.id = os.market_id
            WHERE os.outcome != $1
            {date_filter}
            ORDER BY os.created_at DESC
            LIMIT {limit_param}
            """,
            *params,
        )
        result = []
        for r in rows:
            d = dict(r)
            d["markets"] = {"category": d.pop("market_category", None)}
            result.append(d)
        return result

    # =====================================================================
    # Analytics Dashboard v2
    # =====================================================================

    async def get_walk_forward_data(self, days: int = 30) -> list[dict[str, Any]]:
        """Rolling daily Brier: oracle vs market для walk-forward трекера."""
        rows = await fetch(
            """
            SELECT
                DATE(created_at) as date,
                COUNT(*) as n,
                AVG(POWER(oracle_score - (CASE WHEN outcome = 'yes' THEN 1.0 ELSE 0.0 END), 2)) as brier_oracle,
                AVG(POWER(market_price - (CASE WHEN outcome = 'yes' THEN 1.0 ELSE 0.0 END), 2)) as brier_market,
                AVG(oracle_score) as avg_oracle,
                AVG(market_price) as avg_market,
                AVG(CASE WHEN prediction_correct THEN 1.0 ELSE 0.0 END) as accuracy
            FROM oracle_snapshots
            WHERE outcome IN ('yes', 'no')
              AND resolved_at >= NOW() - INTERVAL '1 day' * $1
            GROUP BY DATE(created_at)
            HAVING COUNT(*) >= 10
            ORDER BY date ASC
            """,
            days,
        )
        return records_to_list(rows)

    async def get_signal_attribution(self, period_days: int | None = None) -> dict[str, Any]:
        """Средний вклад каждого сигнала, сгруппированный по prediction_correct."""
        signal_columns = [
            "whale_contribution_raw",
            "news_contribution_raw",
            "sm_fresh_raw",
            "sm_cluster_raw",
            "sm_registry_raw",
            "oi_factor_raw",
            "concentration_factor_raw",
            "micro_factor_raw",
        ]
        avg_exprs = ", ".join(f"AVG(COALESCE({col}, 0)) AS {col}" for col in signal_columns)

        if period_days is not None:
            query = f"""
                SELECT prediction_correct, COUNT(*) AS cnt, {avg_exprs}
                FROM oracle_snapshots
                WHERE outcome != $1 AND resolved_at >= NOW() - INTERVAL '1 day' * $2
                GROUP BY prediction_correct
            """
            rows = await fetch(query, "pending", period_days)
        else:
            query = f"""
                SELECT prediction_correct, COUNT(*) AS cnt, {avg_exprs}
                FROM oracle_snapshots
                WHERE outcome != $1
                GROUP BY prediction_correct
            """
            rows = await fetch(query, "pending")

        correct_signals: dict[str, float] = {}
        incorrect_signals: dict[str, float] = {}
        total_correct = 0
        total_incorrect = 0

        for row in rows:
            d = dict(row)
            is_correct = d["prediction_correct"]
            count = d["cnt"]
            signals = {col: round(float(d[col]), 6) for col in signal_columns}

            if is_correct:
                correct_signals = signals
                total_correct = count
            else:
                incorrect_signals = signals
                total_incorrect = count

        return {
            "correct": correct_signals,
            "incorrect": incorrect_signals,
            "total_correct": total_correct,
            "total_incorrect": total_incorrect,
        }

    async def get_error_analysis(self, limit: int = 20, period_days: int | None = None) -> dict[str, Any]:
        """Анализ ошибок: худшие прогнозы + распределение типов ошибок."""
        # --- 1. Worst predictions ---
        if period_days is not None:
            worst_query = """
                SELECT os.id, os.market_id, os.oracle_score, os.market_price,
                       os.outcome, os.brier_score, os.divergence, os.created_at, os.resolved_at,
                       m.question, m.category
                FROM oracle_snapshots os
                LEFT JOIN markets m ON m.id = os.market_id
                WHERE os.prediction_correct = FALSE AND os.outcome != $1
                      AND os.resolved_at >= NOW() - INTERVAL '1 day' * $2
                ORDER BY os.brier_score DESC
                LIMIT $3
            """
            worst_rows = await fetch(worst_query, "pending", period_days, limit)
        else:
            worst_query = """
                SELECT os.id, os.market_id, os.oracle_score, os.market_price,
                       os.outcome, os.brier_score, os.divergence, os.created_at, os.resolved_at,
                       m.question, m.category
                FROM oracle_snapshots os
                LEFT JOIN markets m ON m.id = os.market_id
                WHERE os.prediction_correct = FALSE AND os.outcome != $1
                ORDER BY os.brier_score DESC
                LIMIT $2
            """
            worst_rows = await fetch(worst_query, "pending", limit)

        worst_predictions = records_to_list(worst_rows)

        # --- 2. Error distribution ---
        if period_days is not None:
            dist_query = """
                SELECT
                    SUM(CASE WHEN os.oracle_score >= 0.7 AND os.outcome = 'no' THEN 1 ELSE 0 END)
                        AS overconfident_yes,
                    SUM(CASE WHEN os.oracle_score <= 0.3 AND os.outcome = 'yes' THEN 1 ELSE 0 END)
                        AS overconfident_no,
                    SUM(CASE WHEN os.oracle_score > 0.3 AND os.oracle_score < 0.7
                             AND os.prediction_correct = FALSE THEN 1 ELSE 0 END)
                        AS uncertain_wrong,
                    SUM(CASE WHEN os.prediction_correct = FALSE THEN 1 ELSE 0 END) AS total_errors,
                    SUM(CASE WHEN os.prediction_correct = TRUE THEN 1 ELSE 0 END) AS total_correct,
                    SUM(CASE WHEN os.prediction_correct = TRUE
                         AND ABS(os.oracle_score - os.market_price) < 0.05
                         THEN 1 ELSE 0 END) AS lagging_correct,
                    COUNT(*) AS total_resolved
                FROM oracle_snapshots os
                WHERE os.outcome != $1 AND os.resolved_at >= NOW() - INTERVAL '1 day' * $2
            """
            dist_rows = await fetch(dist_query, "pending", period_days)
        else:
            dist_query = """
                SELECT
                    SUM(CASE WHEN os.oracle_score >= 0.7 AND os.outcome = 'no' THEN 1 ELSE 0 END)
                        AS overconfident_yes,
                    SUM(CASE WHEN os.oracle_score <= 0.3 AND os.outcome = 'yes' THEN 1 ELSE 0 END)
                        AS overconfident_no,
                    SUM(CASE WHEN os.oracle_score > 0.3 AND os.oracle_score < 0.7
                             AND os.prediction_correct = FALSE THEN 1 ELSE 0 END)
                        AS uncertain_wrong,
                    SUM(CASE WHEN os.prediction_correct = FALSE THEN 1 ELSE 0 END) AS total_errors,
                    SUM(CASE WHEN os.prediction_correct = TRUE THEN 1 ELSE 0 END) AS total_correct,
                    SUM(CASE WHEN os.prediction_correct = TRUE
                         AND ABS(os.oracle_score - os.market_price) < 0.05
                         THEN 1 ELSE 0 END) AS lagging_correct,
                    COUNT(*) AS total_resolved
                FROM oracle_snapshots os
                WHERE os.outcome != $1
            """
            dist_rows = await fetch(dist_query, "pending")

        dist_row = dist_rows[0] if dist_rows else {}
        error_distribution = {
            "overconfident_yes": int(dist_row.get("overconfident_yes") or 0),
            "overconfident_no": int(dist_row.get("overconfident_no") or 0),
            "uncertain_wrong": int(dist_row.get("uncertain_wrong") or 0),
            "total_errors": int(dist_row.get("total_errors") or 0),
            "total_correct": int(dist_row.get("total_correct") or 0),
            "lagging_correct": int(dist_row.get("lagging_correct") or 0),
            "total_resolved": int(dist_row.get("total_resolved") or 0),
        }

        # --- 3. Lagging predictions: верные прогнозы с малой дивергенцией ---
        if period_days is not None:
            lagging_query = """
                SELECT os.id, os.market_id, os.oracle_score, os.market_price,
                       os.divergence, os.created_at,
                       m.question, m.category
                FROM oracle_snapshots os
                LEFT JOIN markets m ON m.id = os.market_id
                WHERE os.prediction_correct = TRUE AND os.outcome != $1
                      AND ABS(os.oracle_score - os.market_price) < 0.05
                      AND os.resolved_at >= NOW() - INTERVAL '1 day' * $2
                ORDER BY ABS(os.divergence) ASC
                LIMIT $3
            """
            lagging_rows = await fetch(lagging_query, "pending", period_days, limit)
        else:
            lagging_query = """
                SELECT os.id, os.market_id, os.oracle_score, os.market_price,
                       os.divergence, os.created_at,
                       m.question, m.category
                FROM oracle_snapshots os
                LEFT JOIN markets m ON m.id = os.market_id
                WHERE os.prediction_correct = TRUE AND os.outcome != $1
                      AND ABS(os.oracle_score - os.market_price) < 0.05
                ORDER BY ABS(os.divergence) ASC
                LIMIT $2
            """
            lagging_rows = await fetch(lagging_query, "pending", limit)

        lagging_predictions = records_to_list(lagging_rows)

        return {
            "worst_predictions": worst_predictions,
            "lagging_predictions": lagging_predictions,
            "error_distribution": error_distribution,
        }

    async def get_whale_attribution(self, limit: int = 20, min_txs: int = 3) -> list[dict[str, Any]]:
        """Атрибуция китов: win rate, объёмы, expert_weight по resolved рынкам."""
        query = """
            WITH market_outcomes AS (
                SELECT market_id, outcome
                FROM mv_market_resolved_outcome
            )
            SELECT
                t.whale_id,
                w.address,
                w.display_name,
                COUNT(*) AS total_txs,
                SUM(CASE
                    WHEN (LOWER(t.outcome) = 'yes' OR t.outcome = m.token_outcomes->>'Yes')
                         AND mo.outcome = 'yes'
                    THEN 1
                    WHEN (LOWER(t.outcome) = 'no' OR t.outcome = m.token_outcomes->>'No')
                         AND mo.outcome = 'no'
                    THEN 1
                    ELSE 0
                END) AS correct_txs,
                SUM(t.amount) AS total_amount,
                AVG(t.amount) AS avg_amount,
                w.expert_weight
            FROM transactions t
            JOIN market_outcomes mo ON mo.market_id = t.market_id
            LEFT JOIN whales w ON w.id = t.whale_id
            LEFT JOIN markets m ON m.id = t.market_id
            WHERE t.outcome IS NOT NULL AND t.amount >= 5000
            GROUP BY t.whale_id, w.address, w.display_name, w.expert_weight
            HAVING COUNT(*) >= $1
            ORDER BY COUNT(*) DESC
            LIMIT $2
        """
        rows = await fetch(query, min_txs, limit)

        result = []
        for r in rows:
            whale_id = r["whale_id"]
            address = r["address"] or ""
            display_name = r["display_name"]
            total_txs = int(r["total_txs"])
            correct_txs = int(r["correct_txs"])
            total_amount = float(r["total_amount"]) if r["total_amount"] is not None else 0.0
            avg_amount = float(r["avg_amount"]) if r["avg_amount"] is not None else 0.0
            expert_weight = float(r["expert_weight"]) if r["expert_weight"] is not None else 0.0

            # Маскировка адреса: 0x1234...cdef
            if len(address) >= 10:
                masked_address = address[:6] + "..." + address[-4:]
            else:
                masked_address = address

            result.append(
                {
                    "whale_id": str(whale_id) if whale_id else None,
                    "masked_address": masked_address,
                    "display_name": display_name,
                    "total_txs": total_txs,
                    "correct_txs": correct_txs,
                    "win_rate": round(correct_txs / total_txs * 100, 1) if total_txs > 0 else 0.0,
                    "total_amount": round(total_amount, 2),
                    "avg_amount": round(avg_amount, 2),
                    "expert_weight": round(expert_weight, 4),
                }
            )
        return result

    async def get_category_breakdown(self, period_days: int | None = None) -> list[dict[str, Any]]:
        """Разбивка метрик по категориям рынков."""
        if period_days is not None:
            query = """
                SELECT
                    COALESCE(m.category, 'Unknown') AS category,
                    COUNT(*) AS total,
                    SUM(CASE WHEN os.prediction_correct THEN 1 ELSE 0 END) AS correct,
                    AVG(os.brier_score) AS avg_brier
                FROM oracle_snapshots os
                LEFT JOIN markets m ON m.id = os.market_id
                WHERE os.outcome != $1 AND os.resolved_at >= NOW() - INTERVAL '1 day' * $2
                GROUP BY COALESCE(m.category, 'Unknown')
                ORDER BY total DESC
            """
            rows = await fetch(query, "pending", period_days)
        else:
            query = """
                SELECT
                    COALESCE(m.category, 'Unknown') AS category,
                    COUNT(*) AS total,
                    SUM(CASE WHEN os.prediction_correct THEN 1 ELSE 0 END) AS correct,
                    AVG(os.brier_score) AS avg_brier
                FROM oracle_snapshots os
                LEFT JOIN markets m ON m.id = os.market_id
                WHERE os.outcome != $1
                GROUP BY COALESCE(m.category, 'Unknown')
                ORDER BY total DESC
            """
            rows = await fetch(query, "pending")

        result = []
        for r in rows:
            total = r["total"]
            correct = r["correct"]
            result.append(
                {
                    "category": r["category"],
                    "total": total,
                    "correct": correct,
                    "accuracy": round(correct / total, 4) if total > 0 else 0.0,
                    "avg_brier": round(float(r["avg_brier"]), 4) if r["avg_brier"] is not None else 0.0,
                }
            )
        return result

    async def get_ece_mce(self, period_days: int | None = None) -> dict[str, Any]:
        """ECE и MCE калибровки. Использует resolved snapshots."""
        base = "WHERE outcome != $1"
        params: list[Any] = ["pending"]
        if period_days is not None:
            base += " AND resolved_at >= NOW() - INTERVAL '1 day' * $2"
            params.append(period_days)

        rows = await fetch(
            f"""
            SELECT
                LEAST(FLOOR(oracle_score::numeric * 10)::int, 9) AS bin_idx,
                ROUND(AVG(oracle_score), 4) AS confidence,
                ROUND(AVG(CASE WHEN outcome = 'yes' THEN 1.0 ELSE 0.0 END), 4) AS accuracy,
                COUNT(*) AS count
            FROM oracle_snapshots
            {base}
            GROUP BY bin_idx
            ORDER BY bin_idx
            """,
            *params,
        )

        if not rows:
            return {"ece": 0.0, "mce": 0.0, "worst_bin": "", "bins": []}

        total_n = sum(row["count"] for row in rows)
        ece = 0.0
        mce = 0.0
        worst_bin = ""
        bin_results = []

        for row in rows:
            conf = float(row["confidence"])
            acc = float(row["accuracy"])
            gap = abs(acc - conf)
            count = row["count"]
            label = f"{row['bin_idx'] * 0.1:.1f}-{row['bin_idx'] * 0.1 + 0.1:.1f}"

            ece += (count / total_n) * gap
            if gap > mce:
                mce = gap
                worst_bin = label

            bin_results.append(
                {
                    "bin": label,
                    "confidence": round(conf, 4),
                    "accuracy": round(acc, 4),
                    "gap": round(gap, 4),
                    "count": count,
                }
            )

        return {
            "ece": round(ece, 4),
            "mce": round(mce, 4),
            "worst_bin": worst_bin,
            "bins": bin_results,
        }

    async def get_brier_skill_score(self, period_days: int | None = None) -> dict[str, Any]:
        """BSS = 1 - BS_model / BS_market. Murphy decomposition: reliability, resolution, uncertainty.

        Сравнивает oracle_score (модель) с market_price (reference) на resolved рынках.
        BSS > 0 → модель лучше рынка, BSS = 0 → равны, BSS < 0 → хуже рынка.

        SQL aggregation: 2 запроса вместо fetch ALL + 3 Python-циклов.
        """
        base = "WHERE outcome != $1 AND outcome IS NOT NULL AND oracle_score IS NOT NULL AND market_price IS NOT NULL"
        params: list[Any] = ["pending"]
        if period_days is not None:
            base += " AND resolved_at >= NOW() - INTERVAL '1 day' * $2"
            params.append(period_days)

        # 1. Глобальные агрегаты: bs_model, bs_market, base_rate, n
        outcome_expr = "CASE WHEN outcome = 'yes' THEN 1.0 ELSE 0.0 END"
        global_row = await fetchrow(
            f"""
            SELECT
                COUNT(*) AS n,
                AVG(POWER(oracle_score - {outcome_expr}, 2)) AS bs_model,
                AVG(POWER(market_price - {outcome_expr}, 2)) AS bs_market,
                AVG({outcome_expr}) AS base_rate
            FROM oracle_snapshots {base}
            """,
            *params,
        )

        n = global_row["n"]
        if n == 0:
            return {
                "bss": 0.0,
                "bs_model": 0.0,
                "bs_market": 0.0,
                "reliability": 0.0,
                "resolution": 0.0,
                "uncertainty": 0.0,
                "n": 0,
                "n_bins": 0,
            }

        bs_model = float(global_row["bs_model"])
        bs_market = float(global_row["bs_market"])
        base_rate = float(global_row["base_rate"])
        bss = 1.0 - (bs_model / bs_market) if bs_market > 0 else 0.0
        uncertainty = base_rate * (1.0 - base_rate)

        # 2. Биннинг: reliability и resolution по 10 бинам
        bin_rows = await fetch(
            f"""
            SELECT
                LEAST(FLOOR(oracle_score::numeric * 10)::int, 9) AS bin_idx,
                COUNT(*) AS n_k,
                AVG(oracle_score) AS f_k,
                AVG({outcome_expr}) AS o_k
            FROM oracle_snapshots {base}
            GROUP BY bin_idx
            """,
            *params,
        )

        reliability = 0.0
        resolution = 0.0
        for row in bin_rows:
            n_k = row["n_k"]
            f_k = float(row["f_k"])
            o_k = float(row["o_k"])
            reliability += (n_k / n) * (f_k - o_k) ** 2
            resolution += (n_k / n) * (o_k - base_rate) ** 2

        return {
            "bss": round(bss, 4),
            "bs_model": round(bs_model, 4),
            "bs_market": round(bs_market, 4),
            "reliability": round(reliability, 4),
            "resolution": round(resolution, 4),
            "uncertainty": round(uncertainty, 4),
            "n": n,
            "n_bins": len(bin_rows),
        }

    async def get_signal_woe_iv(self, period_days: int | None = None) -> dict[str, Any]:
        """WoE/IV для каждого из 8 сигналов.

        Returns dict с ключами: sample_size (int) + по одному ключу на каждый сигнал.
        """
        signal_columns = [
            "whale_contribution_raw",
            "news_contribution_raw",
            "sm_fresh_raw",
            "sm_cluster_raw",
            "sm_registry_raw",
            "oi_factor_raw",
            "concentration_factor_raw",
            "micro_factor_raw",
        ]
        cols = ", ".join(f"COALESCE({c}, 0) AS {c}" for c in signal_columns)

        if period_days is not None:
            query = (
                f"SELECT prediction_correct, {cols} FROM oracle_snapshots "
                "WHERE outcome != $1 AND resolved_at >= NOW() - INTERVAL '1 day' * $2"
            )
            rows = await fetch(query, "pending", period_days)
        else:
            query = f"SELECT prediction_correct, {cols} FROM oracle_snapshots WHERE outcome != $1"
            rows = await fetch(query, "pending")

        sample_size = len(rows)

        result: dict[str, Any] = {"sample_size": sample_size}
        for col in signal_columns:
            result[col] = self._compute_woe_iv(rows, col)

        return result

    @staticmethod
    def _compute_woe_iv(rows: list[dict], signal_col: str) -> dict[str, Any]:
        """Вычислить WoE/IV для одного сигнала."""
        empty: dict[str, Any] = {"iv": 0.0, "strength": "useless", "bins": []}
        if not rows:
            return empty

        values = [float(r[signal_col]) for r in rows]
        outcomes = [bool(r["prediction_correct"]) for r in rows]

        total_correct = sum(1 for o in outcomes if o)
        total_incorrect = len(outcomes) - total_correct
        if total_correct == 0 or total_incorrect == 0:
            return empty

        # Sparse data fallback: >80% zeros → binary binning
        zero_pct = sum(1 for v in values if v == 0.0) / len(values)
        if zero_pct > 0.80:
            bins_def = [(0, "=0", lambda v: v == 0.0), (1, ">0", lambda v: v > 0.0)]
            return OracleSnapshotRepository._compute_iv_from_bins(
                values,
                outcomes,
                bins_def,
                total_correct,
                total_incorrect,
            )

        # Квинтили (5 бинов)
        sorted_vals = sorted(values)
        n = len(sorted_vals)
        boundaries = []
        for q in range(1, 5):
            idx = int(n * q / 5)
            boundaries.append(sorted_vals[min(idx, n - 1)])

        # Убираем дубли
        boundaries = sorted(set(boundaries))

        def make_bins(bounds: list[float]) -> list[tuple]:
            result_bins: list[tuple] = []
            bound_0 = bounds[0]
            result_bins.append((0, f"≤{bounds[0]:.3f}", lambda v, b=bound_0: v <= b))
            for i in range(len(bounds) - 1):
                lo_b, hi_b = bounds[i], bounds[i + 1]
                label = f"{lo_b:.3f}-{hi_b:.3f}"
                result_bins.append((i + 1, label, lambda v, lo=lo_b, hi=hi_b: lo < v <= hi))
            bound_last = bounds[-1]
            result_bins.append((len(bounds), f">{bounds[-1]:.3f}", lambda v, b=bound_last: v > b))
            return result_bins

        if not boundaries:
            return empty

        bins_def = make_bins(boundaries)
        return OracleSnapshotRepository._compute_iv_from_bins(
            values,
            outcomes,
            bins_def,
            total_correct,
            total_incorrect,
        )

    @staticmethod
    def _compute_iv_from_bins(
        values: list[float],
        outcomes: list[bool],
        bins_def: list[tuple],
        total_correct: int,
        total_incorrect: int,
    ) -> dict[str, Any]:
        """Вычислить IV из определённых бинов."""
        import math

        EPS = 0.0001
        iv_total = 0.0
        bin_results = []

        for _, label, predicate in bins_def:
            correct_in_bin = sum(1 for v, o in zip(values, outcomes) if predicate(v) and o)
            incorrect_in_bin = sum(1 for v, o in zip(values, outcomes) if predicate(v) and not o)
            count = correct_in_bin + incorrect_in_bin

            if count == 0:
                continue

            pct_correct = (correct_in_bin + EPS) / (total_correct + EPS)
            pct_incorrect = (incorrect_in_bin + EPS) / (total_incorrect + EPS)
            woe = math.log(pct_correct / pct_incorrect)
            iv_contrib = (pct_correct - pct_incorrect) * woe
            iv_total += iv_contrib

            bin_results.append(
                {
                    "range": label,
                    "woe": round(woe, 4),
                    "count": count,
                    "correct_pct": round(correct_in_bin / count, 4) if count > 0 else 0.0,
                    "incorrect_pct": round(incorrect_in_bin / count, 4) if count > 0 else 0.0,
                }
            )

        iv_total = round(iv_total, 4)

        if iv_total < 0.02:
            strength = "useless"
        elif iv_total < 0.1:
            strength = "weak"
        elif iv_total < 0.3:
            strength = "medium"
        elif iv_total < 0.5:
            strength = "strong"
        else:
            strength = "suspicious"

        return {"iv": iv_total, "strength": strength, "bins": bin_results}

    async def get_resolved_for_strategies(self, period_days: int = 14) -> list[dict]:
        """Resolved snapshots с divergence и prediction_correct для strategy filtering."""
        rows = await fetch(
            """
            SELECT id, market_id, oracle_score, market_price, divergence,
                   prediction_correct, outcome, brier_score, created_at, resolved_at,
                   whale_contribution_raw, news_contribution_raw
            FROM oracle_snapshots
            WHERE outcome IS NOT NULL
              AND outcome != $1
              AND resolved_at >= NOW() - $2 * INTERVAL '1 day'
            ORDER BY resolved_at DESC
            """,
            "pending",
            period_days,
        )
        return [dict(r) for r in rows]

    async def get_historical_report_data(self, before_date) -> dict[str, Any]:
        """Данные для Historical Report: resolved snapshots + pending count + weekly timeline."""
        import asyncio
        from datetime import time

        before_ts = datetime.combine(before_date, time(23, 59, 59), tzinfo=UTC)

        async def fetch_resolved():
            query = """
                SELECT os.id, os.market_id, os.oracle_score, os.market_price,
                       os.outcome, os.divergence, os.created_at, os.resolved_at,
                       m.question, m.category, m.end_date, m.event_id,
                       COALESCE(os.volume_24h, m.volume_24h, 0) AS volume_24h
                FROM oracle_snapshots os
                LEFT JOIN markets m ON m.id = os.market_id
                WHERE os.outcome != $1
                  AND os.resolved_at <= $2
                ORDER BY os.created_at ASC
            """
            rows = await fetch(query, "pending", before_ts)
            return records_to_list(rows)

        async def fetch_pending_count():
            return await fetchval(
                """
                SELECT COUNT(*) FROM oracle_snapshots
                WHERE created_at <= $1
                  AND (resolved_at IS NULL OR resolved_at > $1)
                """,
                before_ts,
            )

        async def fetch_weekly_timeline():
            return await fetch(
                """
                SELECT date_trunc('week', resolved_at)::date AS week,
                       COUNT(*) AS resolved,
                       AVG(CASE WHEN prediction_correct THEN 1.0 ELSE 0.0 END) AS accuracy,
                       AVG(brier_score) AS brier
                FROM oracle_snapshots
                WHERE outcome != $1
                  AND resolved_at <= $2
                GROUP BY date_trunc('week', resolved_at)
                ORDER BY week ASC
                """,
                "pending",
                before_ts,
            )

        resolved, pending_count, timeline = await asyncio.gather(
            fetch_resolved(), fetch_pending_count(), fetch_weekly_timeline()
        )

        return {
            "resolved_snapshots": resolved,
            "pending_at_date": pending_count or 0,
            "timeline": [
                {
                    "week": str(row["week"]),
                    "accuracy": float(row["accuracy"] or 0),
                    "brier": float(row["brier"] or 0),
                    "resolved": int(row["resolved"]),
                }
                for row in timeline
            ],
        }


# ─── Historical Report helpers (module-level) ────────────────────────────────


def _compute_historical_summary(snapshots: list[dict]) -> dict:
    """Вычислить summary из resolved snapshots."""
    if not snapshots:
        return {"total_resolved": 0, "accuracy": 0.0, "avg_brier_score": 0.0}

    total = len(snapshots)
    correct = sum(
        1
        for s in snapshots
        if (float(s["oracle_score"]) >= 0.5 and str(s.get("outcome", "no")).lower() == "yes")
        or (float(s["oracle_score"]) < 0.5 and str(s.get("outcome", "no")).lower() != "yes")
    )
    brier_scores = []
    for s in snapshots:
        outcome_binary = 1.0 if str(s.get("outcome", "no")).lower() == "yes" else 0.0
        score = float(s["oracle_score"])
        brier_scores.append((score - outcome_binary) ** 2)

    return {
        "total_resolved": total,
        "accuracy": correct / total if total else 0.0,
        "avg_brier_score": sum(brier_scores) / total if total else 0.0,
    }


def _compute_calibration_bins(snapshots: list[dict]) -> list[dict]:
    """10 бинов калибровки: predicted vs actual."""
    bins_data: list[dict[str, float]] = [{"predicted_sum": 0.0, "actual_sum": 0.0, "count": 0} for _ in range(10)]
    for s in snapshots:
        score = float(s["oracle_score"])
        idx = min(int(score * 10), 9)
        bins_data[idx]["predicted_sum"] += score
        bins_data[idx]["actual_sum"] += 1.0 if str(s.get("outcome", "no")).lower() == "yes" else 0.0
        bins_data[idx]["count"] += 1

    result = []
    for i, b in enumerate(bins_data):
        if b["count"] > 0:
            result.append(
                {
                    "bin": f"{i / 10:.1f}-{(i + 1) / 10:.1f}",
                    "predicted": b["predicted_sum"] / b["count"],
                    "actual": b["actual_sum"] / b["count"],
                    "count": int(b["count"]),
                }
            )
    return result


def _compute_score_buckets(snapshots: list[dict]) -> list[dict]:
    """10 бакетов по Oracle Score: accuracy, avg brier, avg market price."""
    buckets: list[dict[str, float]] = [
        {"resolved": 0, "wins": 0, "oracle_sum": 0.0, "market_sum": 0.0, "brier_sum": 0.0} for _ in range(10)
    ]
    for s in snapshots:
        score = float(s["oracle_score"])
        idx = min(int(score * 10), 9)
        outcome_binary = 1.0 if str(s.get("outcome", "no")).lower() == "yes" else 0.0
        buckets[idx]["resolved"] += 1
        buckets[idx]["wins"] += 1 if str(s.get("outcome", "no")).lower() == "yes" else 0
        buckets[idx]["oracle_sum"] += score
        buckets[idx]["market_sum"] += float(s.get("market_price", 0) or 0)
        buckets[idx]["brier_sum"] += (score - outcome_binary) ** 2

    result = []
    for i, b in enumerate(buckets):
        if b["resolved"] > 0:
            n = b["resolved"]
            result.append(
                {
                    "bucket": f"{i * 10}-{(i + 1) * 10}%",
                    "resolved": int(n),
                    "wins": int(b["wins"]),
                    "win_rate": round(b["wins"] / n * 100, 1),
                    "avg_market_price": b["market_sum"] / n,
                    "avg_oracle": b["oracle_sum"] / n,
                    "avg_brier": b["brier_sum"] / n,
                }
            )
    return result


def _compute_ece_mce(snapshots: list[dict]) -> dict:
    """ECE и MCE из resolved snapshots."""
    bins = _compute_calibration_bins(snapshots)
    if not bins:
        return {"ece": 0.0, "mce": 0.0}

    total = sum(b["count"] for b in bins)
    ece = sum(b["count"] / total * abs(b["predicted"] - b["actual"]) for b in bins)
    mce = max(abs(b["predicted"] - b["actual"]) for b in bins)
    return {"ece": ece, "mce": mce}
