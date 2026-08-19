"""Phase 0 Portfolio Analyst Agent — replay-кейс 6 (UMA-часть): пересекались ли когда-либо
открытые live-позиции (execution_orders, execution_mode IN ('maker','taker')) с
disputed-статусом UMA-резолюции рынка (mv_uma_dispute_timeline.dispute_count >= 1).

MiCA-часть кейса 6 (Step 7-9 брифа) НЕ входит в этот скрипт — требует web_search на
первоисточнике (ESMA/национальный регулятор), делается отдельной сессией с доступом
к веб-инструментам. См. docs/superpowers/plans/2026-07-20-portfolio-agent-phase0.md,
Task 9 "Steps — MiCA".

Join-ключ (тройной джойн через markets как мост):
    execution_orders.market_id (text, markets.polymarket_id по конвенции CLAUDE.md)
        → markets.polymarket_id (text, UNIQUE)
        → markets.id (uuid, PK)
        → mv_uma_dispute_timeline.market_id (uuid, объявлен в самой миграции
          20260521090451_create_tier2_materialized_views.sql:24 как `m.id AS market_id` —
          несмотря на имя колонки это markets.id, НЕ markets.polymarket_id).
Прямой двухтабличный джойн execution_orders.market_id = mv_uma_dispute_timeline.market_id
невозможен (text vs uuid, разные домены). См. брифа "Обоснование" в task-9-brief.md.

Верифицировано на реальной схеме прода (Step 1 брифа, 2026-07-20, read-only \\d + LIMIT 20
sample), НЕ только статически по миграции:
  - markets.id uuid PK, markets.polymarket_id text UNIQUE — совпадает с ожиданием.
  - mv_uma_dispute_timeline.market_id uuid (UNIQUE index) — совпадает с ожиданием.
  - execution_orders.market_id text — совпадает с ожиданием.
  - Sample 20 последних execution_orders: 20/20 совпали по markets.polymarket_id
    (в этой выборке condition_id тоже совпадал у всех строк — оба сходятся, что не
    противоречит брифу: он предупреждал только что совпадение по condition_id
    негарантированно для ВСЕЙ истории, не что оно невозможно на части строк).
  - РАСХОЖДЕНИЕ С БРИФОМ, найденное на этом шаге: execution_orders.execution_mode
    не имеет значения 'live' — реальные live-ордера кодируются как
    execution_mode IN ('maker', 'taker') (см. app/api/bot.py, app/jobs/v2_execution.py,
    app/jobs/redeem_job.py и др. — единообразная конвенция по всему проекту).
    Буквальный `WHERE eo.execution_mode = 'live'` из Step 2 брифа возвращает 0 строк
    всегда, независимо от факта пересечения — это баг фильтра, не факт об отсутствии
    live-ордеров. Скрипт использует execution_mode IN ('maker', 'taker') вместо
    буквального текста брифа; join-ключ (тройной джойн через markets) при этом
    подтверждён без изменений — расхождение только в фильтре execution_mode, не в
    механике джойна.

Запускать ТОЛЬКО на проде, через read-only роль (Task 2, `oraclepoly_agent_ro`) — см.
`init_db(dsn=get_settings().agent_database_url)` ниже. Через -m (не прямой путь к
файлу — иначе Python кладёт директорию скрипта первой в sys.path и `from app...` падает):
    docker exec oraclepoly-backend python -m scripts.research.audit_case6_uma_dispute_overlap
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import sys
from datetime import UTC, datetime

from app.core.config import get_settings
from app.services.database import close_db, execute, fetch, fetchval, init_db

_RUN_NAME = "uma_dispute_overlap_replay"
_JOIN_KEY_USED = (
    "execution_orders.market_id (text) = markets.polymarket_id (text) "
    "-> markets.id (uuid) = mv_uma_dispute_timeline.market_id (uuid)"
)

# Реальные значения execution_mode для live-ордеров (см. докстринг выше — 'live' как
# буквальная строка в execution_orders не встречается ни разу в истории проекта).
LIVE_EXECUTION_MODES = ("maker", "taker")
LIVE_TERMINAL_STATUSES = ("filled", "settled")

_JOIN_SQL = """
SELECT eo.id, eo.market_id, eo.status, eo.execution_mode, eo.detector,
       eo.placed_at, eo.settled_at, eo.outcome, eo.pnl, eo.net_pnl,
       eo.stake, eo.bankroll_at_entry,
       mk.id AS markets_uuid, mk.polymarket_id, mk.condition_id, mk.question,
       m.current_status, m.dispute_count, m.proposed_count,
       m.uma_resolution_statuses, m.resolved, m.winner
  FROM execution_orders eo
  JOIN markets mk ON mk.polymarket_id = eo.market_id
  JOIN mv_uma_dispute_timeline m ON m.market_id = mk.id
 WHERE eo.execution_mode = ANY($1)
   AND m.dispute_count >= 1
   AND eo.status = ANY($2)
 ORDER BY eo.placed_at DESC
"""

# Знаменатель для join-coverage: сколько live filled/settled ордеров вообще нашли пару
# в markets + mv_uma_dispute_timeline (независимо от dispute_count) — доказывает, что
# join-механика работает end-to-end на реальных данных, а не просто возвращает 0 строк
# из-за оторванного джойна.
_JOIN_COVERAGE_SQL = """
SELECT count(*) AS n
  FROM execution_orders eo
  JOIN markets mk ON mk.polymarket_id = eo.market_id
  JOIN mv_uma_dispute_timeline m ON m.market_id = mk.id
 WHERE eo.execution_mode = ANY($1)
   AND eo.status = ANY($2)
"""

_LIVE_ORDER_TOTAL_SQL = """
SELECT count(*) AS n
  FROM execution_orders
 WHERE execution_mode = ANY($1) AND status = ANY($2)
"""


def _capital_at_risk(rows: list[dict]) -> float:
    """Сумма stake по overlapping-ордерам — capital-at-risk на момент диспута (Step 3
    брифа). Не bankroll_at_entry (это размер банка на момент входа, не размер самой
    позиции) — та величина попадает в evidence отдельным полем per-order для контекста."""
    return round(sum(float(r["stake"]) for r in rows if r["stake"] is not None), 2)


async def run_audit() -> dict:
    overlapping_rows_raw = await fetch(_JOIN_SQL, list(LIVE_EXECUTION_MODES), list(LIVE_TERMINAL_STATUSES))
    overlapping_rows = [dict(r) for r in overlapping_rows_raw]

    coverage_row = await fetch(_JOIN_COVERAGE_SQL, list(LIVE_EXECUTION_MODES), list(LIVE_TERMINAL_STATUSES))
    join_coverage_n = coverage_row[0]["n"] if coverage_row else 0

    total_row = await fetch(_LIVE_ORDER_TOTAL_SQL, list(LIVE_EXECUTION_MODES), list(LIVE_TERMINAL_STATUSES))
    live_order_total_n = total_row[0]["n"] if total_row else 0

    n_overlap = len(overlapping_rows)
    capital_at_risk_usd = _capital_at_risk(overlapping_rows)

    evidence = [
        {
            "execution_order_id": str(r["id"]),
            "market_id": r["market_id"],
            "question": r["question"],
            "detector": r["detector"],
            "execution_mode": r["execution_mode"],
            "status": r["status"],
            "placed_at": r["placed_at"].isoformat() if r["placed_at"] else None,
            "settled_at": r["settled_at"].isoformat() if r["settled_at"] else None,
            "outcome": r["outcome"],
            "pnl": float(r["pnl"]) if r["pnl"] is not None else None,
            "net_pnl": float(r["net_pnl"]) if r["net_pnl"] is not None else None,
            "stake": float(r["stake"]) if r["stake"] is not None else None,
            "bankroll_at_entry": float(r["bankroll_at_entry"]) if r["bankroll_at_entry"] is not None else None,
            "uma_current_status": r["current_status"],
            "uma_dispute_count": r["dispute_count"],
            "uma_proposed_count": r["proposed_count"],
            "market_resolved": r["resolved"],
            "market_winner": r["winner"],
        }
        for r in overlapping_rows
    ]

    today = datetime.now(UTC).date().isoformat()

    if n_overlap > 0:
        claim_statement = "UMA dispute risk materialized on our live positions"
        claim_status = "active"
        verdict_lines = [
            f"НАЙДЕНО: {n_overlap} live-ордер(ов) (execution_mode IN {LIVE_EXECUTION_MODES}, "
            f"status IN {LIVE_TERMINAL_STATUSES}) исторически пересекались с рынком, у которого "
            f"dispute_count >= 1 в UMA-резолюции.",
            f"capital_at_risk_usd (сумма stake по overlapping-ордерам) = {capital_at_risk_usd}",
            "См. evidence в metrics.overlap_rows для per-order разбора.",
        ]
    else:
        claim_statement = (
            f"no historical overlap observed as of {today}, precedent risk remains "
            "(Ukraine mineral-deal governance attack)"
        )
        claim_status = "active"
        verdict_lines = [
            f"НЕ НАЙДЕНО: 0 live-ордеров (execution_mode IN {LIVE_EXECUTION_MODES}, "
            f"status IN {LIVE_TERMINAL_STATUSES}) исторически пересекались с рынком, "
            "у которого dispute_count >= 1.",
            "Честная формулировка: это НЕ означает 'риска нет вообще' — прецедент "
            "Ukraine mineral-deal governance attack показывает, что UMA dispute risk "
            "реален на других участниках рынка, просто не материализовался конкретно "
            "на наших live-позициях к этой дате.",
            f"Join-coverage: {join_coverage_n}/{live_order_total_n} live filled/settled "
            "ордеров нашли пару в markets + mv_uma_dispute_timeline (доказывает, что "
            "джойн-механика работает end-to-end на реальных данных, а не просто "
            "структурно оторвана от исходных таблиц).",
        ]

    verdict_lines.append(f"join_key_used: {_JOIN_KEY_USED}")
    verdict_lines.append(
        "РАСХОЖДЕНИЕ С БРИФОМ (Step 1 находка): execution_mode='live' как буквальная "
        "строка не существует в execution_orders — использовано execution_mode IN "
        "('maker', 'taker') по общепроектной конвенции (app/api/bot.py и др.). "
        "Join-ключ (тройной джойн через markets) при этом подтверждён без изменений."
    )
    verdict = "\n".join(verdict_lines)

    return {
        "metrics": {
            "overlapping_orders": n_overlap,
            "capital_at_risk_usd": capital_at_risk_usd,
            "join_key_used": _JOIN_KEY_USED,
            "join_coverage_n": join_coverage_n,
            "live_order_total_n": live_order_total_n,
            "live_execution_modes_used": list(LIVE_EXECUTION_MODES),
            "live_terminal_statuses_used": list(LIVE_TERMINAL_STATUSES),
            "overlap_rows": evidence,
        },
        "verdict": verdict,
        "claim_statement": claim_statement,
        "claim_status": claim_status,
    }


async def _dataset_hash() -> str:
    row = await fetch(
        """
        SELECT count(*) AS n, max(eo.placed_at) AS max_placed_at
          FROM execution_orders eo
         WHERE eo.execution_mode = ANY($1) AND eo.status = ANY($2)
        """,
        list(LIVE_EXECUTION_MODES),
        list(LIVE_TERMINAL_STATUSES),
    )
    n = row[0]["n"] if row else 0
    max_placed_at = row[0]["max_placed_at"] if row else None
    return hashlib.sha256(f"{n}:{max_placed_at}".encode()).hexdigest()[:16]


def _config_hash() -> str:
    config = {
        "live_execution_modes": list(LIVE_EXECUTION_MODES),
        "live_terminal_statuses": list(LIVE_TERMINAL_STATUSES),
        "join_key": _JOIN_KEY_USED,
        "dispute_count_threshold": 1,
    }
    return hashlib.sha256(json.dumps(config, sort_keys=True).encode()).hexdigest()[:16]


_INSERT_RUN_SQL = """
INSERT INTO agent_runs (kind, name, config_hash, dataset_hash, query_version, metrics, verdict, finished_at)
VALUES ('audit', $1, $2, $3, $4, $5::jsonb, $6, now())
RETURNING id
"""

_UPSERT_CLAIM_SQL = """
INSERT INTO agent_claims (statement, scope, status, evidence_run_ids, verify_policy, verify_halflife_days)
VALUES ($1, 'policy:uma_dispute_risk', $2, ARRAY[$3]::bigint[], 'event_trigger', NULL)
ON CONFLICT (statement) DO UPDATE
   SET evidence_run_ids = agent_claims.evidence_run_ids || EXCLUDED.evidence_run_ids,
       status = EXCLUDED.status,
       last_verified_at = now(),
       updated_at = now()
"""


async def persist(result: dict, *, query_version: str) -> int:
    """Записать agent_runs + upsert agent_claims. Требует write-права на 4 agent_*
    таблицы — выданы read-only роли `oraclepoly_agent_ro` точечным GRANT (Task 2:
    SELECT/INSERT/UPDATE на agent_claims/agent_runs/agent_actions/agent_llm_calls),
    несмотря на общее название "read-only" (она read-only только на торговые таблицы)."""
    config_hash = _config_hash()
    dataset_hash = await _dataset_hash()

    run_row = await fetch(
        _INSERT_RUN_SQL,
        _RUN_NAME,
        config_hash,
        dataset_hash,
        query_version,
        json.dumps(result["metrics"], default=str),
        result["verdict"],
    )
    run_id = run_row[0]["id"]

    await execute(
        _UPSERT_CLAIM_SQL,
        result["claim_statement"],
        result["claim_status"],
        run_id,
    )

    return run_id


async def _run(*, dry_run: bool) -> dict:
    settings = get_settings()
    dsn = settings.agent_database_url
    if not dsn:
        print(
            "WARNING: agent_database_url не задан — read-only роль oraclepoly_agent_ro "
            "ещё не сконфигурирована (Task 2). Скрипт НЕ должен молча уходить под "
            "основным DATABASE_URL для read-only аудита.",
            file=sys.stderr,
        )
        sys.exit(1)

    await init_db(dsn=dsn)
    try:
        result = await run_audit()
        run_id = None
        if not dry_run:
            query_version = await fetchval("SELECT 1") and "audit_case6_uma_dispute_overlap@1.0.0"
            run_id = await persist(result, query_version=query_version)
        return {**result, "run_id": run_id}
    finally:
        await close_db()


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Посчитать и напечатать метрики, но не писать в agent_runs/agent_claims.",
    )
    args = parser.parse_args()

    result = asyncio.run(_run(dry_run=args.dry_run))

    print(json.dumps(result, indent=2, default=str))


if __name__ == "__main__":
    main()
