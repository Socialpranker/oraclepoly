"""Bin Reliability Repository — доступ к данным калибровочной надёжности по бинам."""

from __future__ import annotations

from typing import Any

from app.services.database import execute, fetch, records_to_list


class BinReliabilityRepository:
    async def get_latest(self) -> list[dict[str, Any]]:
        """Получить последнюю запись по каждому бину."""
        rows = await fetch(
            """
            SELECT DISTINCT ON (bin_label)
                id, bin_start, bin_end, bin_label, sample_count,
                avg_predicted, actual_win_rate, gap, abs_gap,
                is_whitelisted, confidence_lower, confidence_upper, computed_at
            FROM bin_reliability
            ORDER BY bin_label, computed_at DESC
            """
        )
        return records_to_list(rows)

    async def get_whitelisted_bins(self) -> list[str]:
        """Получить список whitelisted bin labels (последние данные)."""
        rows = await fetch(
            """
            SELECT DISTINCT ON (bin_label) bin_label
            FROM bin_reliability
            WHERE is_whitelisted = true
            ORDER BY bin_label, computed_at DESC
            """
        )
        return [r["bin_label"] for r in rows]

    async def insert_bins(self, bins: list[dict[str, Any]]) -> int:
        """Bulk insert bin reliability данных (новый расчёт)."""
        count = 0
        for b in bins:
            await execute(
                """
                INSERT INTO bin_reliability (
                    bin_start, bin_end, bin_label, sample_count,
                    avg_predicted, actual_win_rate, gap, abs_gap,
                    is_whitelisted, confidence_lower, confidence_upper
                ) VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11)
                """,
                b["bin_start"],
                b["bin_end"],
                b["bin_label"],
                b["sample_count"],
                b["avg_predicted"],
                b["actual_win_rate"],
                b["gap"],
                b["abs_gap"],
                b["is_whitelisted"],
                b["confidence_lower"],
                b["confidence_upper"],
            )
            count += 1
        return count
