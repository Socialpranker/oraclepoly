"""Improvement Cards — daily cron (Phase 1, Компонент 4 Portfolio Analyst Agent).

Читает active claims с evidence_run_ids от аудитов Phase 1, извлекает размер
утечки $ (адаптер на тип источника; edge_decay досчитывает своим SQL), считает
EV/час по версионированным тирам, upsert карточек по card_key. Обслуживает
lifecycle (ignored/stale). НЕ отправляет TG — отправка = weekly-бриф (Компонент 5).

Design doc: docs/superpowers/specs/2026-07-21-improvement-cards-design.md
Cron: daily 07:30 UTC (`30 7 * * *`), после adverse-selection-monitor (07:15).
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any

import structlog

from app.analytics.improvement_cards import (
    CardDraft,
    TierConfig,
    build_title,
    classify_source_kind,
    ev_per_hour,
    extract_leak_usd_from_metrics,
    initial_lifecycle_stage,
    resolve_mechanism_ref,
)
from app.services.database import execute, fetch, fetchrow

logger = structlog.get_logger()

_QUERY_VERSION = "improvement_cards_monitor_v1"
_RUN_NAME = "improvement_cards_monitor:run"

_DEFAULT_TIERS: dict[str, dict[str, float]] = {
    "adverse_selection": {"p_fix": 0.6, "hours": 2},
    "edge_decay": {"p_fix": 0.4, "hours": 4},
    "counterfactual": {"p_fix": 0.5, "hours": 3},
}
_DEFAULT_IGNORED_AFTER_DAYS = 30
_DEFAULT_STALE_AFTER_DAYS = 90
# recent-окно для досчёта edge_decay leak — совпадает с recent_days edge_decay (14д).
_EDGE_DECAY_LEAK_DAYS = 14

_CONFIG_SQL = "SELECT tiers, ignored_after_days, stale_after_days FROM improvement_cards_config WHERE id = 1"

# active claims с непустым evidence_run_ids — кандидаты в карточки.
# ORDER BY id — детерминизм при card_key-коллизии (adverse-монитор пишет
# scope без категории для 2+ claim'ов с одинаковым card_key): при
# одинаковом порядке обработки старший claim (max id) обрабатывается
# последним и побеждает upsert предсказуемо, а не в порядке выдачи БД.
_CANDIDATE_CLAIMS_SQL = """
SELECT id, scope, statement, evidence_run_ids
  FROM agent_claims
 WHERE status = 'active'
   AND evidence_run_ids IS NOT NULL
   AND array_length(evidence_run_ids, 1) > 0
 ORDER BY id
"""

# свежайший evidence-run claim'а (max id из массива).
_RUN_BY_ID_SQL = "SELECT id, name, metrics FROM agent_runs WHERE id = $1"

# edge_decay leak: SUM(net_pnl) детектора за recent-окно, virtual-only.
# ГЭП (задокументирован в whole-branch ревью 2026-07-22, не чинить пока нет
# триггера): если source-детектор уже в live, live-утечка НЕ учитывается этим
# SQL (execution_mode='virtual' фильтрует её). Чинить при появлении live
# edge_decay claims — сейчас все edge_decay-источники virtual-only.
_EDGE_DECAY_LEAK_SQL = """
SELECT COALESCE(SUM(eo.net_pnl), 0) AS leak_usd
  FROM execution_orders eo
  LEFT JOIN detector_signals ds ON ds.id = eo.detector_signal_id
 WHERE COALESCE(ds.detector, 'unknown') = $1
   AND eo.execution_mode = 'virtual'
   AND eo.status = 'virtual_settled'
   AND eo.settled_at >= now() - ($2 || ' days')::interval
"""

_UPSERT_CARD_SQL = """
INSERT INTO improvement_cards
    (card_key, source_kind, scope, title, claim_id, evidence_run_ids, mechanism_ref,
     leak_usd, p_fix_success, effort_hours, lifecycle_stage)
VALUES ($1, $2, $3, $4, $5, $6::bigint[], $7, $8, $9, $10, $11)
ON CONFLICT (card_key) DO UPDATE
   SET title = EXCLUDED.title,
       evidence_run_ids = EXCLUDED.evidence_run_ids,
       mechanism_ref = EXCLUDED.mechanism_ref,
       leak_usd = EXCLUDED.leak_usd,
       p_fix_success = EXCLUDED.p_fix_success,
       effort_hours = EXCLUDED.effort_hours,
       -- Стадия ДОЛЖНА обновляться: initial_lifecycle_stage считается по leak_usd,
       -- а leak может досчитаться позже первого INSERT (edge_decay берёт его из
       -- своего SQL, а не из metrics run'а). Прод-инцидент: карточка #2
       -- edge_decay:D38 родилась 22.07 без leak → shadow, leak −$12299 приехал
       -- позже, стадия осталась shadow → weekly-бриф (digest/actionable) её не
       -- видел. Переход ТОЛЬКО вперёд (shadow → digest): 'actionable' ставится
       -- человеком вручную и не должен откатываться до 'digest' ночной джобой,
       -- а digest не должен уезжать обратно в shadow, если leak временно
       -- перестал считаться.
       lifecycle_stage = CASE
           WHEN EXCLUDED.lifecycle_stage = 'digest'
                AND improvement_cards.lifecycle_stage = 'shadow'
           THEN 'digest'
           ELSE improvement_cards.lifecycle_stage
       END,
       last_seen_at = now(),
       updated_at = now()
"""

_INSERT_RUN_SQL = """
INSERT INTO agent_runs (kind, name, config_hash, dataset_hash, query_version, metrics, verdict, finished_at)
VALUES ('monitoring', $1, $2, $3, $4, $5::jsonb, $6, now())
"""


@dataclass(frozen=True)
class _ClaimRow:
    claim_id: int
    scope: str
    statement: str
    evidence_run_ids: list[int]


@dataclass(frozen=True)
class _RunRow:
    run_id: int
    name: str
    metrics: dict


def _parse_tiers(raw: dict[str, dict[str, float]]) -> dict[str, TierConfig]:
    return {
        kind: TierConfig(p_fix_success=float(v["p_fix"]), effort_hours=float(v["hours"])) for kind, v in raw.items()
    }


async def _load_config() -> tuple[dict[str, TierConfig], int, int]:
    row = await fetchrow(_CONFIG_SQL)
    if row is None:
        logger.warning("improvement_cards_config_missing_using_defaults")
        return _parse_tiers(_DEFAULT_TIERS), _DEFAULT_IGNORED_AFTER_DAYS, _DEFAULT_STALE_AFTER_DAYS
    tiers_raw = row["tiers"]
    if isinstance(tiers_raw, str):
        tiers_raw = json.loads(tiers_raw)
    return _parse_tiers(tiers_raw), int(row["ignored_after_days"]), int(row["stale_after_days"])


async def _fetch_candidate_claims() -> list[_ClaimRow]:
    rows = await fetch(_CANDIDATE_CLAIMS_SQL)
    return [
        _ClaimRow(
            claim_id=row["id"],
            scope=row["scope"],
            statement=row["statement"],
            evidence_run_ids=list(row["evidence_run_ids"]),
        )
        for row in rows
    ]


async def _fetch_run(run_id: int) -> _RunRow | None:
    row = await fetchrow(_RUN_BY_ID_SQL, run_id)
    if row is None:
        return None
    metrics = row["metrics"]
    if isinstance(metrics, str):
        metrics = json.loads(metrics)
    return _RunRow(run_id=row["id"], name=row["name"], metrics=metrics or {})


def _edge_decay_leak_or_none(summed_net_pnl: float) -> float | None:
    """SUM(net_pnl) < 0 → утечка (как есть); >= 0 → None (детектор не течёт → digest)."""
    return summed_net_pnl if summed_net_pnl < 0 else None


async def _fetch_edge_decay_leak(detector: str, days: int) -> float | None:
    row = await fetchrow(_EDGE_DECAY_LEAK_SQL, detector, str(days))
    if row is None:
        return None
    return _edge_decay_leak_or_none(float(row["leak_usd"]))


def _scope_detector(scope: str) -> str:
    """'detector:D38_resolution_lag' → 'D38_resolution_lag'."""
    return scope.split(":", 1)[1] if ":" in scope else scope


async def _upsert_card(draft: CardDraft) -> None:
    await execute(
        _UPSERT_CARD_SQL,
        draft.card_key,
        draft.source_kind,
        draft.scope,
        draft.title,
        draft.claim_id,
        draft.evidence_run_ids,
        draft.mechanism_ref,
        draft.leak_usd,
        draft.p_fix_success,
        draft.effort_hours,
        draft.lifecycle_stage,
    )


# lifecycle-обслуживание: pending дольше N дней → ignored; протухшие claim/карточки → stale.
_MARK_IGNORED_SQL = """
UPDATE improvement_cards
   SET attention_status = 'ignored', updated_at = now()
 WHERE attention_status = 'pending'
   AND status = 'open'
   AND first_seen_at < now() - ($1 || ' days')::interval
"""

_MARK_STALE_SQL = """
UPDATE improvement_cards ic
   SET status = 'stale', updated_at = now()
 WHERE ic.status = 'open'
   AND (
        ic.last_seen_at < now() - ($1 || ' days')::interval
     OR EXISTS (
            SELECT 1 FROM agent_claims c
             WHERE c.id = ic.claim_id
               AND c.status IN ('contradicted', 'superseded')
        )
   )
"""


async def _reconcile_lifecycle(ignored_after_days: int, stale_after_days: int) -> tuple[int, int]:
    ignored_res = await execute(_MARK_IGNORED_SQL, str(ignored_after_days))
    stale_res = await execute(_MARK_STALE_SQL, str(stale_after_days))
    return _rowcount(ignored_res), _rowcount(stale_res)


def _rowcount(execute_result: str) -> int:
    """asyncpg execute() возвращает строку вида 'UPDATE 3' — берём число."""
    parts = execute_result.split()
    return int(parts[-1]) if parts and parts[-1].isdigit() else 0


async def _build_draft(claim: _ClaimRow, tiers: dict[str, TierConfig]) -> CardDraft | None:
    """Собрать карточку из claim + свежайшего evidence-run. None если не аудит-источник."""
    latest_run_id = max(claim.evidence_run_ids)
    run = await _fetch_run(latest_run_id)
    if run is None:
        return None
    source_kind = classify_source_kind(run.name)
    if source_kind is None:
        return None

    leak_usd = extract_leak_usd_from_metrics(source_kind, run.metrics)
    if source_kind == "edge_decay" and leak_usd is None:
        # edge_decay может писать methodology-scoped claims (scope не "detector:X",
        # а "methodology:...") — per-detector leak-досчёт _fetch_edge_decay_leak
        # (WHERE ds.detector = $1) к ним неприменим (_scope_detector вернёт
        # methodology-строку целиком, детектора с таким именем не будет). Лог вместо
        # молчания: leak остаётся None → карточка уходит в shadow-lifecycle, но факт
        # пропуска досчёта виден в логах, а не тонет.
        if claim.scope.startswith("methodology:"):
            logger.info("improvement_card_methodology_scope_skipped_leak", scope=claim.scope)
        else:
            leak_usd = await _fetch_edge_decay_leak(_scope_detector(claim.scope), _EDGE_DECAY_LEAK_DAYS)

    tier = tiers.get(source_kind, TierConfig(p_fix_success=0.5, effort_hours=3.0))
    is_temperature = bool(run.metrics.get("is_temperature", False))
    return CardDraft(
        card_key=f"{source_kind}:{claim.scope}",
        source_kind=source_kind,
        scope=claim.scope,
        title=build_title(source_kind, claim.scope, leak_usd),
        claim_id=claim.claim_id,
        evidence_run_ids=claim.evidence_run_ids,
        mechanism_ref=resolve_mechanism_ref(source_kind, claim.scope, is_temperature),
        leak_usd=leak_usd,
        p_fix_success=tier.p_fix_success,
        effort_hours=tier.effort_hours,
        lifecycle_stage=initial_lifecycle_stage(leak_usd),
    )


async def _persist_run(summary: dict[str, Any]) -> None:
    config_hash = f"tiers={summary.get('n_source_kinds', 0)}"
    dataset_hash = f"candidates={summary.get('n_candidates', 0)}"
    await execute(
        _INSERT_RUN_SQL,
        _RUN_NAME,
        config_hash,
        dataset_hash,
        _QUERY_VERSION,
        json.dumps(summary),
        "ok",
    )


async def run_improvement_cards_monitor() -> dict[str, Any]:
    tiers, ignored_after_days, stale_after_days = await _load_config()
    claims = await _fetch_candidate_claims()

    n_upserted = 0
    n_key_collisions = 0
    top_ev: float | None = None
    # card_key -> claim_id последнего claim'а, чей draft был апсёрчен под этим ключом.
    # Коллизия (adverse-монитор пишет scope без категории → 2+ claim'а с одним
    # card_key) → последний upsert молча затирает предыдущий; здесь делаем это видимым.
    seen_card_keys: dict[str, int] = {}
    for claim in claims:
        try:
            draft = await _build_draft(claim, tiers)
            if draft is None:
                continue
            prev_claim_id = seen_card_keys.get(draft.card_key)
            if prev_claim_id is not None:
                n_key_collisions += 1
                logger.warning(
                    "improvement_card_key_collision",
                    card_key=draft.card_key,
                    claim_id=draft.claim_id,
                    prev_claim_id=prev_claim_id,
                )
            seen_card_keys[draft.card_key] = draft.claim_id
            await _upsert_card(draft)
            n_upserted += 1
            draft_ev = ev_per_hour(draft.leak_usd, TierConfig(draft.p_fix_success, draft.effort_hours))
            if draft_ev is not None and (top_ev is None or draft_ev > top_ev):
                top_ev = draft_ev
        except Exception:
            logger.exception("improvement_card_failed", claim_id=claim.claim_id, scope=claim.scope)
            continue

    n_ignored, n_stale = await _reconcile_lifecycle(ignored_after_days, stale_after_days)

    summary: dict[str, Any] = {
        "status": "ok",
        "n_candidates": len(claims),
        "n_cards_upserted": n_upserted,
        "n_ignored": n_ignored,
        "n_stale": n_stale,
        "n_source_kinds": len(tiers),
        "top_ev": top_ev,
        "n_key_collisions": n_key_collisions,
    }
    await _persist_run(summary)
    return summary
