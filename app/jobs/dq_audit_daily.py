"""DQ-мини-аудит — daily cron (Phase 2 C0).

Design doc: docs/superpowers/specs/2026-07-23-portfolio-agent-phase2-policy-engine-design.md §2.1, §3.

4 проверки против прода: freshness (markets/detector_signals), completeness
(активные рынки: end_date, clob_token_ids), EXPIRED-share (доля среди resolved
за lookback-окно; 100% = маркер ID-бага M2/D45), referential integrity
(сироты detector_signals.market_id → markets.polymarket_id).

Пороги — из policy_rules(rule_key='dq_audit', active) с fallback на defaults.
Пишет agent_runs(kind='dq'); при AMBER/RED — agent_claims. TG-🟡 ТОЛЬКО при
смене вердикта против прошлого запуска (throttle, метрика медленная).

Cron: daily 06:45 UTC (`45 6 * * *`) — ДО edge-decay-monitor (07:00), чтобы
policy engine C1 (после мониторов) читал DQ текущего утра.
"""

from __future__ import annotations

import json
from typing import Any

import asyncpg
import structlog

from app.analytics.dq_audit import (
    VERDICT_GREEN,
    DqCheckResult,
    DqThresholds,
    classify_expired_share,
    classify_freshness,
    classify_share,
    overall_verdict,
)
from app.analytics.notification import Notification, Severity
from app.services.database import execute, fetch, fetchrow
from app.services.notification_bus import publish

logger = structlog.get_logger()

_QUERY_VERSION = "dq_audit_daily_v1"
_RUN_NAME = "dq_audit_daily"

# Fallback = seed миграции 20260723120000 (держать в синхроне при смене версии правила)
_DEFAULT_PARAMS: dict[str, Any] = {
    "freshness_markets_amber_minutes": 120,
    "freshness_markets_red_minutes": 720,
    "freshness_signals_amber_minutes": 360,
    "freshness_signals_red_minutes": 1440,
    "completeness_end_date_amber_pct": 15.0,
    "completeness_end_date_red_pct": 40.0,
    "completeness_tokens_amber_pct": 5.0,
    "completeness_tokens_red_pct": 20.0,
    "expired_share_amber_pct": 30.0,
    "expired_share_red_pct": 60.0,
    "expired_min_resolved": 50,
    "ri_orphan_amber_pct": 1.0,
    "ri_orphan_red_pct": 5.0,
    "lookback_days": 7,
}

_RULE_SQL = "SELECT version, params FROM policy_rules WHERE rule_key = 'dq_audit' AND active LIMIT 1"

_FRESHNESS_SQL = """
SELECT EXTRACT(EPOCH FROM (now() - max(m.updated_at))) / 60.0    AS markets_age_min,
       (SELECT EXTRACT(EPOCH FROM (now() - max(ds.generated_at))) / 60.0
          FROM detector_signals ds)                              AS signals_age_min
  FROM markets m
"""

_COMPLETENESS_SQL = """
SELECT count(*)                                                     AS active_total,
       count(*) FILTER (WHERE end_date IS NULL)                     AS no_end_date,
       count(*) FILTER (WHERE clob_token_ids = '[]'::jsonb)         AS no_tokens
  FROM markets
 WHERE is_active = true
"""

_EXPIRED_SQL = """
SELECT count(*) FILTER (WHERE outcome = 'EXPIRED')                  AS expired,
       count(*) FILTER (WHERE outcome IN ('WIN', 'LOSS', 'EXPIRED')) AS resolved_total
  FROM detector_signals
 WHERE generated_at > now() - make_interval(days => $1)
"""

# ID-конвенция: market_id = markets.polymarket_id (0x), НИКОГДА markets.id UUID (инцидент M2/D45)
_RI_SQL = """
SELECT count(*) FILTER (WHERE m.polymarket_id IS NULL)              AS orphans,
       count(*)                                                     AS total
  FROM detector_signals ds
  LEFT JOIN markets m ON m.polymarket_id = ds.market_id
 WHERE ds.generated_at > now() - make_interval(days => $1)
"""

_INSERT_RUN_SQL = """
INSERT INTO agent_runs (kind, name, config_hash, dataset_hash, query_version, metrics, verdict, finished_at)
VALUES ('dq', $1, $2, $3, $4, $5::jsonb, $6, now())
RETURNING id
"""

_UPSERT_CLAIM_SQL = """
INSERT INTO agent_claims (statement, scope, status, evidence_run_ids, verify_policy, verify_halflife_days)
VALUES ($1, $2, 'active', ARRAY[$3]::bigint[], 'time_halflife', 7)
ON CONFLICT (statement) DO UPDATE
   SET evidence_run_ids = agent_claims.evidence_run_ids || EXCLUDED.evidence_run_ids,
       last_verified_at = now(),
       updated_at = now()
"""

_PREV_VERDICT_SQL = """
SELECT verdict
  FROM agent_runs
 WHERE name = $1 AND kind = 'dq'
 ORDER BY started_at DESC
 LIMIT 1
"""


async def _load_rule() -> tuple[int, DqThresholds]:
    """Активное правило dq_audit из policy_rules; version=0 = fallback defaults."""
    try:
        row = await fetchrow(_RULE_SQL)
    except asyncpg.UndefinedTableError:
        # policy_rules ещё не создана (миграция 20260723120000 не применена/
        # откачена) — та же fallback-ветка, что при пустой/неактивной таблице,
        # чтобы джоба не падала целиком (whole-branch review Fix 4).
        logger.warning("dq_audit_rule_table_missing_using_defaults")
        return 0, DqThresholds.from_params(_DEFAULT_PARAMS)
    if row is None:
        logger.warning("dq_audit_rule_missing_using_defaults")
        return 0, DqThresholds.from_params(_DEFAULT_PARAMS)
    params = row["params"]
    if isinstance(params, str):
        params = json.loads(params)
    return int(row["version"]), DqThresholds.from_params(params)


async def _check_freshness(t: DqThresholds) -> list[DqCheckResult]:
    row = await fetchrow(_FRESHNESS_SQL)
    markets_age = float(row["markets_age_min"]) if row and row["markets_age_min"] is not None else None
    signals_age = float(row["signals_age_min"]) if row and row["signals_age_min"] is not None else None
    return [
        classify_freshness(markets_age, t.freshness_markets_amber_minutes, t.freshness_markets_red_minutes, "markets"),
        classify_freshness(signals_age, t.freshness_signals_amber_minutes, t.freshness_signals_red_minutes, "signals"),
    ]


async def _check_completeness(t: DqThresholds) -> list[DqCheckResult]:
    row = await fetchrow(_COMPLETENESS_SQL)
    total = int(row["active_total"]) if row else 0
    if total == 0:
        # активных рынков ноль — это freshness-катастрофа, ловится freshness-RED;
        # completeness на пустом множестве не осмысленна
        return [
            classify_share(
                "completeness:end_date",
                100.0,
                t.completeness_end_date_amber_pct,
                t.completeness_end_date_red_pct,
                "активных рынков 0",
            ),
        ]
    no_end_date_pct = 100.0 * int(row["no_end_date"]) / total
    no_tokens_pct = 100.0 * int(row["no_tokens"]) / total
    return [
        classify_share(
            "completeness:end_date",
            no_end_date_pct,
            t.completeness_end_date_amber_pct,
            t.completeness_end_date_red_pct,
            f"без end_date {row['no_end_date']}/{total} = {no_end_date_pct:.1f}%",
        ),
        classify_share(
            "completeness:tokens",
            no_tokens_pct,
            t.completeness_tokens_amber_pct,
            t.completeness_tokens_red_pct,
            f"без clob_token_ids {row['no_tokens']}/{total} = {no_tokens_pct:.1f}%",
        ),
    ]


async def _check_expired_share(t: DqThresholds) -> DqCheckResult:
    row = await fetchrow(_EXPIRED_SQL, t.lookback_days)
    expired = int(row["expired"]) if row else 0
    resolved_total = int(row["resolved_total"]) if row else 0
    return classify_expired_share(expired, resolved_total, t)


async def _check_referential_integrity(t: DqThresholds) -> DqCheckResult:
    row = await fetchrow(_RI_SQL, t.lookback_days)
    total = int(row["total"]) if row else 0
    orphans = int(row["orphans"]) if row else 0
    orphan_pct = 100.0 * orphans / total if total > 0 else 0.0
    return classify_share(
        "ri:orphans",
        orphan_pct,
        t.ri_orphan_amber_pct,
        t.ri_orphan_red_pct,
        f"сирот market_id {orphans}/{total} = {orphan_pct:.2f}% (эталон: markets.polymarket_id)",
    )


async def _persist_run(results: list[DqCheckResult], verdict: str, rule_version: int, t: DqThresholds) -> int:
    metrics = {
        "rule_version": rule_version,
        "lookback_days": t.lookback_days,
        "checks": [
            {
                "check": r.check,
                "verdict": r.verdict,
                "measured": r.measured,
                "amber": r.amber_threshold,
                "red": r.red_threshold,
                "detail": r.detail,
            }
            for r in results
        ],
    }
    config_hash = f"dq_audit:v{rule_version}"
    dataset_hash = f"checks={len(results)}:lookback={t.lookback_days}d"
    run_row = await fetch(
        _INSERT_RUN_SQL, _RUN_NAME, config_hash, dataset_hash, _QUERY_VERSION, json.dumps(metrics), verdict
    )
    return run_row[0]["id"]


async def _persist_claim_if_degraded(verdict: str, run_id: int) -> None:
    if verdict == VERDICT_GREEN:
        return
    # Statement стабилен (без вердикта в тексте) — иначе AMBER и RED дают два
    # разных ON CONFLICT(statement) claim'а, оба вечно активные (whole-branch
    # review Fix 3). Сам вердикт уже хранится в agent_runs.verdict, доступен
    # через evidence_run_ids — дублировать его в тексте claim'а не нужно.
    statement = "dq audit degraded"
    await execute(_UPSERT_CLAIM_SQL, statement, "policy:dq_audit", run_id)


async def _previous_verdict() -> str | None:
    row = await fetchrow(_PREV_VERDICT_SQL, _RUN_NAME)
    return row["verdict"] if row else None


_CHECK_LABELS = {
    "freshness:markets": "свежесть рынков",
    "freshness:signals": "свежесть сигналов",
    "completeness:end_date": "заполненность дат окончания",
    "completeness:tokens": "заполненность токенов",
    "expired_share": "доля просроченных сигналов",
    "ri:orphans": "осиротевшие записи",
}


def to_notifications(verdict: str, previous: str | None, results: list[DqCheckResult]) -> list[Notification]:
    """Смена вердикта DQ → Notification (спека §3).

    Возврат в GREEN — это FYI с ОТДЕЛЬНЫМ kind, а не ACTION_REQUIRED: делать
    нечего, и главное — общий kind означал бы, что «данные снова в порядке»
    подавлено 25-часовым cooldown после «данные испортились». Та же ловушка,
    что с recovery у proxy_health_check.

    what_if_ignored=None: у плохих данных нет цены в долларах на руках. Она
    зависит от того, какие решения на этих данных примут, — §3 запрещает
    выдумывать такую цифру.
    """
    degraded = [r for r in results if r.verdict != VERDICT_GREEN]

    if verdict == VERDICT_GREEN:
        return [
            Notification(
                severity=Severity.FYI,
                subject="infra:data_quality",
                kind="dq_recovery",
                headline="Качество данных вернулось в норму",
                what_happened=[f"Проверки данных снова проходят целиком (было: {previous or '—'})."],
                what_if_ignored=None,
                what_to_do=None,
                actions=[],
                evidence=[f"{r.check}={r.verdict}" for r in results],
            )
        ]

    lines = [
        f"Проверки данных ухудшились: было {previous or '—'}, стало {verdict}.",
    ]
    for r in degraded:
        label = _CHECK_LABELS.get(r.check, r.check)
        lines.append(f"Не в порядке {label}: {r.detail}.")
    lines.append("На этих данных считаются метрики детекторов — пока они кривые, выводам по ним верить нельзя.")

    return [
        Notification(
            severity=Severity.ACTION_REQUIRED,
            subject="infra:data_quality",
            kind="dq_degraded",
            headline="Данные, на которых всё считается, испортились",
            what_happened=lines,
            what_if_ignored=None,
            what_to_do=(
                "Посмотри, какая проверка сломалась, и почему перестал обновляться её источник: "
                "чаще всего это остановившаяся джоба синхронизации, а не сами данные."
            ),
            actions=[],
            evidence=[
                f"{r.check}: {r.verdict} — {r.detail} (amber≥{r.amber_threshold}, red≥{r.red_threshold})"
                for r in degraded
            ],
        )
    ]


async def run_dq_audit_daily() -> dict[str, Any]:
    rule_version, thresholds = await _load_rule()

    results: list[DqCheckResult] = []
    results.extend(await _check_freshness(thresholds))
    results.extend(await _check_completeness(thresholds))
    results.append(await _check_expired_share(thresholds))
    results.append(await _check_referential_integrity(thresholds))

    verdict = overall_verdict(results)
    previous = await _previous_verdict()

    run_id = await _persist_run(results, verdict, rule_version, thresholds)
    await _persist_claim_if_degraded(verdict, run_id)

    tg_sent = False
    if previous != verdict:
        # Throttle по смене вердикта остаётся здесь (решение владельца): он
        # событийный и точнее конвертного 25-часового окна — держащийся RED
        # не повторяется вовсе, а смена RED→AMBER проходит сразу.
        #
        # Побочно закрыто известное ограничение (Fix 2, коммит acc68636):
        # send_telegram_message не возвращала bool, и tg_sent означал лишь
        # «await не бросил исключение». Шина ходит через
        # send_telegram_with_keyboard_rows, у которой честный bool, поэтому
        # PublishResult.sent — реальное подтверждение отправки.
        published = await publish(to_notifications(verdict, previous, results), actor="dq_audit_daily")
        tg_sent = published.sent > 0 or published.digested > 0

    logger.info(
        "dq_audit_done",
        verdict=verdict,
        previous=previous,
        run_id=run_id,
        rule_version=rule_version,
        n_checks=len(results),
        tg_sent=tg_sent,
    )
    return {
        "status": "ok",
        "verdict": verdict,
        "previous": previous,
        "run_id": run_id,
        "rule_version": rule_version,
        "n_checks": len(results),
        "tg_sent": tg_sent,
    }
