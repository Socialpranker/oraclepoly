"""Adverse selection intel — daily cron.

Design doc: docs/superpowers/specs/2026-07-21-adverse-selection-intel-design.md

Для каждой пары detector×category (окно lookback_days) считает Wilson↓ и
per-event net_pnl из event_performance view, помечает adverse (проигрыш
направления + убыток), пишет agent_runs(kind='monitoring')/agent_claims,
шлёт один сводный TG-алерт ТОЛЬКО при изменении набора adverse-ячеек
(throttle — метрика медленная). Никакого auto-blacklist — только сигнал.

Cron: daily 07:15 UTC (`15 7 * * *`), после edge-decay-monitor (07:00).
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import (
    UTC,
    datetime,
    timedelta,
)
from typing import Any

import structlog

from app.analytics.adverse_selection import (
    AdverseCell,
    AdverseSelectionConfig,
    classify_cell,
)
from app.analytics.adverse_selection_formatter import to_notifications
from app.services.database import execute, fetch, fetchrow
from app.services.notification_bus import publish

logger = structlog.get_logger()

_DEFAULT_CONFIG_ROW = {"lookback_days": 60, "min_events": 20, "wilson_threshold": 0.50}
_CONFIG_SQL = "SELECT lookback_days, min_events, wilson_threshold FROM adverse_selection_monitor_config WHERE id = 1"
_QUERY_VERSION = "adverse_selection_monitor_v1"
_MANIFEST_NAME = "adverse_selection_monitor:run_manifest"


async def _load_config() -> AdverseSelectionConfig:
    row = await fetchrow(_CONFIG_SQL)
    if row is None:
        logger.warning("adverse_selection_config_missing_using_defaults")
        row = _DEFAULT_CONFIG_ROW
    return AdverseSelectionConfig(
        min_events=int(row["min_events"]),
        wilson_threshold=float(row["wilson_threshold"]),
    )


async def _load_lookback_days() -> int:
    row = await fetchrow(_CONFIG_SQL)
    if row is None:
        return int(_DEFAULT_CONFIG_ROW["lookback_days"])
    return int(row["lookback_days"])


@dataclass(frozen=True)
class _CellRow:
    detector: str
    category: str
    outcome: str  # WIN/LOSS = знак event net_pnl
    net_pnl: float  # event net_pnl (сумма ордеров события)
    is_temperature: bool  # bool_or по markets события


# Event-clustered per (detector, category): одна строка на СОБЫТИЕ (markets.event_id),
# не на ордер — чтобы не переоценивать категории с внутрисобытийной корреляцией
# (BTC-рынки = 32-38 коррелированных входов = одна ставка; см. event_performance view,
# migrations/20260707093221_category_performance_views.sql). Единица = событие (design §10).
#
# outcome события = знак суммарного net_pnl события (WIN если >0, LOSS если <0);
# события с net_pnl=0 отброшены (неинформативны, как EXPIRED). Wilson-ряд строится
# на этих event-outcome'ах. net_pnl события = SUM(net_pnl) FILTER (outcome<>'EXPIRED').
#
# Только virtual (execution_mode='virtual'): якорь (§8) и sibling category_health.py —
# virtual-only; live/virtual экономики несовместимы (docs/detector_lifecycle.md).
#
# is_temperature: bool_or SQL-LIKE по markers — ДЕРЖАТЬ В СИНХРОНЕ с
# app/analytics/adverse_selection.py::_TEMPERATURE_MARKERS.
_CELL_ROWS_SQL = """
WITH event_clusters AS (
    SELECT COALESCE(ds.detector, 'unknown')                    AS detector,
           COALESCE(m.category, 'other')                       AS category,
           m.event_id                                          AS event_id,
           SUM(eo.net_pnl) FILTER (WHERE eo.outcome <> 'EXPIRED') AS event_net_pnl,
           bool_or(
               lower(m.question) LIKE ANY (
                   ARRAY['%temperature%', '%°f%', '%°c%', '%ºf%', '%ºc%']
               )
           )                                                   AS is_temperature
      FROM execution_orders eo
      JOIN markets m ON m.polymarket_id = eo.market_id
      LEFT JOIN detector_signals ds ON ds.id = eo.detector_signal_id
     WHERE eo.status = 'virtual_settled'
       AND eo.execution_mode = 'virtual'
       AND eo.outcome IN ('WIN', 'LOSS', 'EARLY_EXIT', 'EXPIRED')
       AND eo.settled_at >= $1
       AND m.event_id IS NOT NULL
     GROUP BY 1, 2, m.event_id
)
SELECT detector,
       category,
       CASE WHEN event_net_pnl > 0 THEN 'WIN' ELSE 'LOSS' END AS outcome,
       event_net_pnl                                          AS net_pnl,
       is_temperature                                         AS is_temperature
  FROM event_clusters
 WHERE event_net_pnl IS NOT NULL
   AND event_net_pnl <> 0
 ORDER BY detector, category
"""


def _now() -> datetime:
    return datetime.now(UTC)


async def _fetch_cell_rows(since: datetime) -> list[_CellRow]:
    rows = await fetch(_CELL_ROWS_SQL, since)
    return [
        _CellRow(
            detector=row["detector"],
            category=row["category"],
            outcome=row["outcome"],
            net_pnl=float(row["net_pnl"]) if row["net_pnl"] is not None else 0.0,
            is_temperature=bool(row["is_temperature"]),
        )
        for row in rows
    ]


def _build_cells(rows: list[_CellRow], config: AdverseSelectionConfig) -> list[AdverseCell]:
    grouped: dict[tuple[str, str], list[_CellRow]] = {}
    for row in rows:
        grouped.setdefault((row.detector, row.category), []).append(row)

    cells: list[AdverseCell] = []
    for (detector, category), cell_rows in grouped.items():
        outcomes = [r.outcome for r in cell_rows]
        pnls = [r.net_pnl for r in cell_rows]
        net_pnl = sum(pnls)
        is_temp = any(r.is_temperature for r in cell_rows)
        cells.append(
            classify_cell(
                detector=detector,
                category=category,
                outcomes=outcomes,
                pnls=pnls,
                net_pnl=net_pnl,
                is_temperature=is_temp,
                config=config,
            )
        )
    return cells


def _cell_key(cell: AdverseCell) -> str:
    return f"{cell.detector}×{cell.category}"


def _adverse_set(cells: list[AdverseCell]) -> frozenset[str]:
    return frozenset(_cell_key(c) for c in cells if c.is_adverse)


_INSERT_RUN_SQL = """
INSERT INTO agent_runs (kind, name, config_hash, dataset_hash, query_version, metrics, verdict, finished_at)
VALUES ('monitoring', $1, $2, $3, $4, $5::jsonb, $6, now())
RETURNING id
"""

_UPSERT_CLAIM_SQL = """
INSERT INTO agent_claims (statement, scope, status, evidence_run_ids, verify_policy, verify_halflife_days)
VALUES ($1, $2, 'active', ARRAY[$3]::bigint[], 'time_halflife', 14)
ON CONFLICT (statement) DO UPDATE
   SET evidence_run_ids = agent_claims.evidence_run_ids || EXCLUDED.evidence_run_ids,
       last_verified_at = now(),
       updated_at = now()
"""

_PREV_MANIFEST_SQL = """
SELECT metrics
  FROM agent_runs
 WHERE name = $1 AND kind = 'monitoring'
 ORDER BY started_at DESC
 LIMIT 1
"""

_INSERT_MANIFEST_SQL = """
INSERT INTO agent_runs (kind, name, config_hash, dataset_hash, query_version, metrics, verdict, finished_at)
VALUES ('monitoring', $1, 'manifest', 'manifest', $2, $3::jsonb, 'manifest', now())
"""


async def _persist_cell_run(cell: AdverseCell, config: AdverseSelectionConfig) -> int:
    metrics = {
        "detector": cell.detector,
        "category": cell.category,
        "n_events": cell.n_events,
        "wins": cell.wins,
        "wilson_lower": cell.wilson_lower,
        "wilson_upper": cell.wilson_upper,
        "net_pnl": cell.net_pnl,
        "is_temperature": cell.is_temperature,
        "is_adverse": cell.is_adverse,
    }
    config_hash = f"minN={config.min_events}_wthr={config.wilson_threshold}"
    dataset_hash = f"{cell.detector}×{cell.category}:{cell.n_events}"
    verdict = "adverse" if cell.is_adverse else "clean"
    name = f"adverse_selection_monitor:{cell.detector}×{cell.category}"
    run_row = await fetch(
        _INSERT_RUN_SQL, name, config_hash, dataset_hash, _QUERY_VERSION, json.dumps(metrics), verdict
    )
    return run_row[0]["id"]


async def _persist_claim_if_adverse(cell: AdverseCell, run_id: int) -> None:
    if not cell.is_adverse:
        return
    statement = f"adverse selection detected for {cell.detector}×{cell.category}"
    scope = f"detector:{cell.detector}"
    await execute(_UPSERT_CLAIM_SQL, statement, scope, run_id)


async def _previous_adverse_set() -> frozenset[str]:
    row = await fetchrow(_PREV_MANIFEST_SQL, _MANIFEST_NAME)
    if row is None:
        return frozenset()
    metrics = row["metrics"]
    if isinstance(metrics, str):
        metrics = json.loads(metrics)
    return frozenset(metrics.get("adverse_set", []))


async def _persist_manifest(adverse_keys: frozenset[str]) -> None:
    metrics = {"adverse_set": sorted(adverse_keys)}
    await execute(_INSERT_MANIFEST_SQL, _MANIFEST_NAME, _QUERY_VERSION, json.dumps(metrics))


async def run_adverse_selection_monitor() -> dict[str, Any]:
    selection_config = await _load_config()
    lookback_days = await _load_lookback_days()
    since = _now() - timedelta(days=lookback_days)

    rows = await _fetch_cell_rows(since)
    cells = _build_cells(rows, selection_config)

    for cell in cells:
        try:
            run_id = await _persist_cell_run(cell, selection_config)
            await _persist_claim_if_adverse(cell, run_id)
        except Exception:
            logger.exception("adverse_selection_cell_failed", detector=cell.detector, category=cell.category)
            continue

    current = _adverse_set(cells)
    previous = await _previous_adverse_set()
    added = sorted(current - previous)
    removed = sorted(previous - current)
    await _persist_manifest(current)

    tg_sent = False
    if added or removed:
        # Throttle по смене НАБОРА остаётся здесь (решение владельца): он
        # событийный и точнее конвертного окна — держащийся список не
        # повторяется вовсе, а новая связка проходит сразу.
        published = await publish(
            to_notifications(cells, added=added, removed=removed),
            actor="adverse_selection_monitor",
        )
        tg_sent = published.sent > 0 or published.digested > 0

    return {
        "status": "ok",
        "n_cells": len(cells),
        "n_adverse": len(current),
        "added": added,
        "removed": removed,
        "tg_sent": tg_sent,
    }
