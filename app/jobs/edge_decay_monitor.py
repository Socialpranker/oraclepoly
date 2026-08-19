"""Edge decay / regime monitor — daily cron.

Design doc: docs/superpowers/specs/2026-07-21-edge-decay-regime-monitor-design.md

Для каждого активного детектора и методологического класса agent_claims
сравнивает rolling Wilson/Sortino baseline-окна с recent-окном, детектирует
formal changepoint через CUSUM (app/analytics/changepoint.py), пишет
agent_runs(kind='monitoring')/agent_claims на decaying-сущности, шлёт один
сводный TG-алерт. Никакого auto-pause/auto-demote — только сигнал, решение
всегда за человеком через TG (см. docs/detector_lifecycle.md).

Cron: daily 07:00 UTC (`0 7 * * *`), после model-drift-check (05:00 UTC) и
после agent-daily-brief (06:50 UTC); detector-slo-monitor крутится отдельно
каждый час в :20 (`20 * * * *`), а не раз в сутки.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

import structlog

from app.analytics.changepoint import CusumDetector
from app.analytics.edge_decay_alert_formatter import (
    EntityDecayResult,
    is_decaying,
    to_notifications,
)
from app.analytics.edge_metrics import (
    compute_window_metrics,
    synthetic_pnl,
)
from app.services.database import execute, fetch, fetchrow
from app.services.detector_metrics import fetch_active_detectors
from app.services.notification_bus import publish

logger = structlog.get_logger()

_DEFAULT_CONFIG_ROW = {"baseline_days": 90, "recent_days": 14, "cusum_k": 0.5, "cusum_threshold": 5.0}

_CONFIG_SQL = "SELECT baseline_days, recent_days, cusum_k, cusum_threshold FROM edge_decay_monitor_config WHERE id = 1"


@dataclass(frozen=True)
class EdgeDecayConfig:
    baseline_days: int
    recent_days: int
    cusum_k: float
    cusum_threshold: float


async def _load_config() -> EdgeDecayConfig:
    row = await fetchrow(_CONFIG_SQL)
    if row is None:
        logger.warning("edge_decay_config_missing_using_defaults")
        row = _DEFAULT_CONFIG_ROW
    return EdgeDecayConfig(
        baseline_days=int(row["baseline_days"]),
        recent_days=int(row["recent_days"]),
        cusum_k=float(row["cusum_k"]),
        cusum_threshold=float(row["cusum_threshold"]),
    )


_METHODOLOGY_SCOPES_SQL = (
    "SELECT DISTINCT scope FROM agent_claims WHERE scope LIKE 'methodology:%' AND status = 'active'"
)

_LATEST_RUN_FOR_CLAIM_SQL = """
SELECT ar.metrics
  FROM agent_claims ac
  JOIN agent_runs ar ON ar.id = ac.evidence_run_ids[array_upper(ac.evidence_run_ids, 1)]
 WHERE ac.scope = $1
 ORDER BY ar.finished_at DESC NULLS LAST
 LIMIT 1
"""


@dataclass(frozen=True)
class MonitoredEntity:
    name: str
    kind: str  # "detector" | "methodology"
    detectors: list[str]


async def _collect_detector_entities() -> list[MonitoredEntity]:
    """Наблюдаемые детекторы — через SSoT fetch_active_detectors.

    Раньше здесь дублировалось голое `db_active & cron_active`, из-за чего
    WSS-realtime детекторы (M2 и др.) выпадали из надзора: cron-строки у них нет
    by design, детекция идёт в WSS-потоке. SSoT-функция добавляет к пересечению
    whitelist таких детекторов — теперь способ запуска не решает, следим мы за
    детектором или нет (тот же класс бага, что пропуск D49 в cron-маппинге).
    """
    effective = await fetch_active_detectors()
    return [MonitoredEntity(name=det, kind="detector", detectors=[det]) for det in sorted(effective)]


async def _collect_methodology_entities() -> list[MonitoredEntity]:
    scope_rows = await fetch(_METHODOLOGY_SCOPES_SQL)
    entities: list[MonitoredEntity] = []
    for row in scope_rows:
        scope = row["scope"]
        run_row = await fetchrow(_LATEST_RUN_FOR_CLAIM_SQL, scope)
        if run_row is None:
            logger.info("edge_decay_no_data_source", scope=scope)
            continue
        metrics = run_row["metrics"]
        if isinstance(metrics, str):
            metrics = json.loads(metrics)
        detectors = metrics.get("detectors")
        if not detectors:
            logger.info("edge_decay_no_data_source", scope=scope)
            continue
        entities.append(MonitoredEntity(name=scope, kind="methodology", detectors=list(detectors)))
    return entities


_OUTCOME_ROWS_SQL = """
SELECT ds.outcome,
       eo.net_pnl,
       ds.current_price,
       ds.side,
       ds.generated_at
  FROM detector_signals ds
  LEFT JOIN execution_orders eo ON eo.detector_signal_id = ds.id
 WHERE ds.detector = ANY($1::text[])
   AND ds.resolved = true
   AND ds.outcome IN ('WIN', 'LOSS')
   AND ds.generated_at >= $2
 ORDER BY ds.generated_at
"""


@dataclass(frozen=True)
class OutcomeRow:
    outcome: str
    pnl: float
    generated_at: datetime


def _now() -> datetime:
    return datetime.now(UTC)


async def _fetch_outcome_rows(detectors: list[str], since: datetime) -> list[OutcomeRow]:
    rows = await fetch(_OUTCOME_ROWS_SQL, detectors, since)
    result = []
    for row in rows:
        if row["net_pnl"] is not None:
            pnl = float(row["net_pnl"])
        else:
            entry_price = float(row["current_price"]) if row["side"] == "YES" else 1.0 - float(row["current_price"])
            pnl = synthetic_pnl(entry_price, row["outcome"])
        result.append(OutcomeRow(outcome=row["outcome"], pnl=pnl, generated_at=row["generated_at"]))
    return result


def _split_baseline_recent(rows: list[OutcomeRow], recent_days: int) -> tuple[list[OutcomeRow], list[OutcomeRow]]:
    cutoff = _now() - timedelta(days=recent_days)
    baseline = [r for r in rows if r.generated_at < cutoff]
    recent = [r for r in rows if r.generated_at >= cutoff]
    return baseline, recent


_INSERT_RUN_SQL = """
INSERT INTO agent_runs (kind, name, config_hash, dataset_hash, query_version, metrics, verdict, finished_at)
VALUES ('monitoring', $1, $2, $3, $4, $5::jsonb, $6, now())
RETURNING id
"""

_QUERY_VERSION = "edge_decay_monitor_v1"

_UPSERT_CLAIM_SQL = """
INSERT INTO agent_claims (statement, scope, status, evidence_run_ids, verify_policy, verify_halflife_days)
VALUES ($1, $2, 'active', ARRAY[$3]::bigint[], 'time_halflife', 7)
ON CONFLICT (statement) DO UPDATE
   SET evidence_run_ids = agent_claims.evidence_run_ids || EXCLUDED.evidence_run_ids,
       last_verified_at = now(),
       updated_at = now()
"""


def _entity_run_name(entity: EntityDecayResult) -> str:
    return f"edge_decay_monitor:{entity.entity_kind}:{entity.entity_name}"


def _json_safe_sortino(sortino: float | None) -> float | str | None:
    """JSON/JSONB не поддерживают Infinity (Postgres отклоняет 'Infinity' как невалидный
    JSON-токен) — тот же паттерн, что detector_metrics.py/promote_gates.py."""
    return "inf" if sortino == float("inf") else sortino


async def _persist_entity_run(entity: EntityDecayResult, config: EdgeDecayConfig) -> int:
    metrics = {
        "baseline": {
            "n": entity.baseline.n,
            "wins": entity.baseline.wins,
            "wilson_lower": entity.baseline.wilson_lower,
            "wilson_upper": entity.baseline.wilson_upper,
            "sortino": _json_safe_sortino(entity.baseline.sortino),
        },
        "recent": {
            "n": entity.recent.n,
            "wins": entity.recent.wins,
            "wilson_lower": entity.recent.wilson_lower,
            "wilson_upper": entity.recent.wilson_upper,
            "sortino": _json_safe_sortino(entity.recent.sortino),
        },
        "wilson_changepoint": {
            "triggered": entity.wilson_changepoint.triggered,
            "cusum_statistic": entity.wilson_changepoint.cusum_statistic,
            "direction": entity.wilson_changepoint.direction,
        },
        "sortino_changepoint": {
            "triggered": entity.sortino_changepoint.triggered,
            "cusum_statistic": entity.sortino_changepoint.cusum_statistic,
            "direction": entity.sortino_changepoint.direction,
        },
        "detectors": entity.entity_name if entity.entity_kind == "detector" else None,
    }
    config_hash = (
        f"baseline={config.baseline_days}d_recent={config.recent_days}d_k={config.cusum_k}_t={config.cusum_threshold}"
    )
    dataset_hash = f"{entity.entity_name}:{entity.baseline.n}+{entity.recent.n}"
    verdict = "decaying" if is_decaying(entity) else "stable"

    run_row = await fetch(
        _INSERT_RUN_SQL,
        _entity_run_name(entity),
        config_hash,
        dataset_hash,
        _QUERY_VERSION,
        json.dumps(metrics),
        verdict,
    )
    return run_row[0]["id"]


async def _persist_claim_if_decaying(entity: EntityDecayResult, run_id: int) -> None:
    if not is_decaying(entity):
        return
    statement = f"edge decay detected for {entity.entity_name}"
    scope = f"detector:{entity.entity_name}" if entity.entity_kind == "detector" else entity.entity_name
    await execute(_UPSERT_CLAIM_SQL, statement, scope, run_id)


async def _build_entity_result(entity: MonitoredEntity, config: EdgeDecayConfig) -> EntityDecayResult:
    since = _now() - timedelta(days=config.baseline_days + config.recent_days)
    rows = await _fetch_outcome_rows(entity.detectors, since)
    baseline_rows, recent_rows = _split_baseline_recent(rows, config.recent_days)

    baseline_metrics = compute_window_metrics([r.outcome for r in baseline_rows], [r.pnl for r in baseline_rows])
    recent_metrics = compute_window_metrics([r.outcome for r in recent_rows], [r.pnl for r in recent_rows])

    detector = CusumDetector(k=config.cusum_k, threshold=config.cusum_threshold)
    baseline_win_series = [1.0 if r.outcome == "WIN" else 0.0 for r in baseline_rows]
    recent_win_series = [1.0 if r.outcome == "WIN" else 0.0 for r in recent_rows]
    wilson_cp = detector.detect(baseline_win_series, recent_win_series)
    sortino_cp = detector.detect([r.pnl for r in baseline_rows], [r.pnl for r in recent_rows])

    return EntityDecayResult(
        entity_name=entity.name,
        entity_kind=entity.kind,
        baseline=baseline_metrics,
        recent=recent_metrics,
        wilson_changepoint=wilson_cp,
        sortino_changepoint=sortino_cp,
    )


async def run_edge_decay_monitor() -> dict[str, Any]:
    config = await _load_config()

    monitored = await _collect_detector_entities() + await _collect_methodology_entities()

    results: list[EntityDecayResult] = []
    for entity in monitored:
        try:
            entity_result = await _build_entity_result(entity, config)
            run_id = await _persist_entity_run(entity_result, config)
            await _persist_claim_if_decaying(entity_result, run_id)
        except Exception:
            logger.exception("edge_decay_entity_failed", entity=entity.name, kind=entity.kind)
            continue
        results.append(entity_result)

    triggered = [e for e in results if is_decaying(e)]

    if triggered:
        notifications = to_notifications(results)
        result = await publish(notifications, actor="edge_decay_monitor")
        logger.info(
            "edge_decay_notifications_published",
            sent=result.sent,
            suppressed=result.suppressed,
            failures=result.failures,
        )

    return {
        "status": "ok",
        "n_entities": len(results),
        "n_triggered": len(triggered),
        "entities": [e.entity_name for e in results],
    }
