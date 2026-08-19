"""DetectorSignalRepository — shadow-mode storage детекторов D1/D3.

Каждый Signal эмитится детектором → INSERT сюда. Раз в час cron resolve
посчитает outcome (WIN/LOSS/EXPIRED) + realized_roi.

Idempotency:
- D1: UNIQUE (detector, evidence->>'tx_hash') предотвращает дубли whale TX
- D3: app-level guard в d3_scan.py — пропускает INSERT если рынок уже в open позиции

ON CONFLICT DO NOTHING — повторный INSERT молча игнорируется.
"""

from __future__ import annotations

import json
from typing import Any

import structlog

from app.detectors.contract import Signal
from app.services.database import execute, fetch

logger = structlog.get_logger()


class DetectorSignalRepository:
    """CRUD для detector_signals."""

    async def insert(self, signal: Signal) -> bool:
        """INSERT signal'а в shadow-mode storage.

        Возвращает True если строка вставлена, False если попала под
        ON CONFLICT (дубль idempotency-ключа).
        """
        evidence_json = json.dumps(signal.evidence)
        result = await execute(
            """
            INSERT INTO detector_signals (
                detector, market_id, event_id, side,
                confidence, expected_edge_pct, fair_price, current_price,
                horizon_sec, token_id, evidence, generated_at
            )
            VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11::jsonb, $12)
            ON CONFLICT DO NOTHING
            """,
            signal.detector,
            signal.market_id,
            signal.event_id,
            signal.side,
            signal.confidence,
            signal.expected_edge_pct,
            signal.fair_price,
            signal.current_price,
            signal.horizon_sec,
            signal.token_id,
            evidence_json,
            signal.generated_at,
        )
        inserted = isinstance(result, str) and result.startswith("INSERT 0 1")
        if not inserted:
            logger.debug(
                "detector_signal_duplicate_skipped",
                detector=signal.detector,
                market_id=signal.market_id,
            )
        return inserted

    async def stats_by_detector(self, hours: int = 24) -> list[dict[str, Any]]:
        """Возвращает per-detector counts + avg confidence за последние hours.

        avg_roi_pct исключает EXPIRED (signals where market не резолвился за
        6×horizon_sec) — они realized_roi=0 by convention, что искажает
        average. Aggregation использует только terminal outcomes (WIN/LOSS).
        """
        return [
            dict(r)
            for r in await fetch(
                """
                SELECT detector,
                       COUNT(*) AS n_total,
                       COUNT(*) FILTER (WHERE resolved = true) AS n_resolved,
                       COUNT(*) FILTER (WHERE outcome = 'WIN') AS n_wins,
                       COUNT(*) FILTER (WHERE outcome = 'LOSS') AS n_losses,
                       COUNT(*) FILTER (WHERE outcome = 'EXPIRED') AS n_expired,
                       AVG(confidence) AS avg_confidence,
                       AVG(realized_roi) FILTER (WHERE outcome IN ('WIN','LOSS')) AS avg_roi_pct
                  FROM detector_signals
                 WHERE generated_at > now() - make_interval(hours => $1)
                 GROUP BY detector
                 ORDER BY n_total DESC
                """,
                hours,
            )
        ]


def get_detector_signal_repository() -> DetectorSignalRepository:
    return DetectorSignalRepository()
