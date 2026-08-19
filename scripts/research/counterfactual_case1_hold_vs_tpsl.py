"""Phase 1 Portfolio Analyst Agent — counterfactual case 1: hold-to-resolution
vs as-is TP/SL, теперь через Counterfactual Engine (app/analytics/counterfactual.py)
вместо разовой mirror-репликации.

Заменяет scripts/research/audit_case3_hold_vs_tpsl_replay.py (та же бизнес-задача),
но:
  - вызывает run_counterfactual(trajectories, baseline=AsIsPolicy(...), variant=HoldToResolutionPolicy(...))
    вместо ручной mirror-симуляции;
  - получает CI из движка (per-event bootstrap, event_cluster_bootstrap_roi) вместо
    голого Sortino;
  - пишет agent_runs/agent_claims тем же паттерном, что все Phase 0 audit-скрипты
    (ON CONFLICT DO UPDATE, fail-loud DSN guard на agent_database_url).

Acceptance (design doc, Testing): реальный прогон на проде воспроизводит число
-83.21% (delta_pct hold vs as-is TP/SL, audit_case3 прод-прогон 2026-07-20),
теперь с CI поверх него.

ВАЖНО про метрики (code review Task 1, зафиксировано здесь чтобы не потерять при
чтении verdict/metrics): `CounterfactualResult.delta_pnl` — среднее per-trajectory
(variant - baseline) по ВСЕМ обработанным trajectories, НЕ сумма в долларах.
`baseline_total_pnl`/`variant_total_pnl` — суммы (портфельный P&L), это и есть
корректные величины для delta_pct/dollar-total отчётности. delta_pct ниже считается
ТОЛЬКО из tpsl_pnl/hold_pnl (алиасы combined.baseline_total_pnl/variant_total_pnl),
никогда из delta_pnl. ci_low/ci_high построены bootstrap'ом вокруг того же
per-trajectory-mean, что и delta_pnl, — НЕ вокруг суммы/delta_pct.

Запускать ТОЛЬКО на проде, через read-only роль oraclepoly_agent_ro. Через -m:
    docker exec oraclepoly-backend python -m scripts.research.counterfactual_case1_hold_vs_tpsl \
        --start 2026-05-28
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import sys
from datetime import UTC, datetime

from app.analytics.counterfactual import run_counterfactual
from app.analytics.policies.as_is import AsIsPolicy
from app.analytics.policies.exit import HoldToResolutionPolicy
from app.core.config import get_settings
from app.services.database import close_db, execute, fetch, init_db
from scripts.research.exit_strategy_backtest import PositionTrajectory, fetch_resolved_with_snapshots

# Верифицированное покрытие снапшотами (то же, что audit_case3_hold_vs_tpsl_replay,
# скопировано а не импортировано — старый модуль superseded, не зависимость нового).
DETECTORS_WITH_SNAPSHOT_COVERAGE = ("D38_resolution_lag", "D42_event_driven_ml")

_RUN_NAME = "counterfactual_hold_vs_tpsl"
_QUERY_VERSION = "counterfactual_case1_hold_vs_tpsl@1.0.0"

_FETCH_ACTUAL_SQL = """
SELECT id, net_pnl::float AS net_pnl, outcome
  FROM execution_orders
 WHERE id = ANY($1::uuid[])
"""


async def _build_actual_lookups(
    trajectories: list[PositionTrajectory],
) -> tuple[dict[str, float], dict[str, str]]:
    """Один batch-запрос net_pnl/outcome по всем order_id — PositionTrajectory
    не несёт эти поля (см. design doc / Task 3 rationale). Proxy-исход
    (EARLY_EXIT/EXPIRED -> LOSS) применяется здесь же, тем же консервативным
    паттерном, что audit_case3/exit_strategy_backtest."""
    order_ids = [traj.order_id for traj in trajectories]
    rows = await fetch(_FETCH_ACTUAL_SQL, order_ids)
    # str(row["id"]) — asyncpg возвращает id как asyncpg.pgproto.pgproto.UUID, а
    # PositionTrajectory.order_id (ключ для .get() у AsIsPolicy/HoldToResolutionPolicy)
    # — plain str (см. fetch_resolved_with_snapshots в exit_strategy_backtest.py:
    # order_id=str(row["id"])). Без приведения типов lookup.get(traj.order_id, default)
    # никогда не находит совпадение — обнаружено прод-прогоном 2026-07-21 (tpsl_pnl=0.0
    # для всех trajectories, юнит-тесты этого не поймали — там fetch мокался словарями
    # с уже строковыми id).
    pnl_lookup = {str(row["id"]): row["net_pnl"] or 0.0 for row in rows}
    outcome_lookup = {
        str(row["id"]): (row["outcome"] if row["outcome"] in ("WIN", "LOSS") else "LOSS") for row in rows
    }
    return pnl_lookup, outcome_lookup


async def run_audit(start_date: datetime) -> dict:
    """Прогнать counterfactual engine по обоим верифицированным детекторам,
    объединить в портфельный агрегат (та же структура ответа, что
    audit_case3_hold_vs_tpsl_replay.run_audit — per_detector + metrics + verdict)."""
    per_detector: dict[str, dict] = {}
    all_trajectories: list[PositionTrajectory] = []

    for detector in DETECTORS_WITH_SNAPSHOT_COVERAGE:
        trajectories = await fetch_resolved_with_snapshots(detector, start_date)
        all_trajectories.extend(trajectories)

        pnl_lookup, outcome_lookup = await _build_actual_lookups(trajectories)
        baseline = AsIsPolicy(actual_net_pnl_fn=lambda traj, lookup=pnl_lookup: lookup.get(traj.order_id, 0.0))
        variant = HoldToResolutionPolicy(
            actual_outcome_fn=lambda traj, lookup=outcome_lookup: lookup.get(traj.order_id, "LOSS")
        )

        detector_result = run_counterfactual(trajectories, baseline=baseline, variant=variant)
        per_detector[detector] = {
            "n_trajectories": detector_result.n,
            "tpsl_pnl": detector_result.baseline_total_pnl,
            "hold_to_resolution_pnl": detector_result.variant_total_pnl,
            # Среднее per-trajectory (variant - baseline), НЕ доля от долларовой суммы
            # выше — не использовать это поле как "разницу в $" за детектор.
            "delta_pnl_mean_per_trajectory": detector_result.delta_pnl,
        }

    pnl_lookup_all, outcome_lookup_all = await _build_actual_lookups(all_trajectories)
    baseline_all = AsIsPolicy(actual_net_pnl_fn=lambda traj, lookup=pnl_lookup_all: lookup.get(traj.order_id, 0.0))
    variant_all = HoldToResolutionPolicy(
        actual_outcome_fn=lambda traj, lookup=outcome_lookup_all: lookup.get(traj.order_id, "LOSS")
    )
    combined = run_counterfactual(all_trajectories, baseline=baseline_all, variant=variant_all)

    # ВАЖНО: только baseline_total_pnl/variant_total_pnl (суммы) идут в delta_pct и
    # dollar-total отчётность. combined.delta_pnl (среднее per-trajectory) НИКОГДА
    # не участвует в этом расчёте — см. модульный docstring выше / code review Task 1.
    tpsl_pnl = combined.baseline_total_pnl
    hold_pnl = combined.variant_total_pnl
    delta_pct = round((hold_pnl - tpsl_pnl) / abs(tpsl_pnl) * 100.0, 2) if tpsl_pnl != 0 else None

    verdict_lines = [
        "Метод: Counterfactual Engine (app/analytics/counterfactual.py) — "
        "run_counterfactual(baseline=AsIsPolicy, variant=HoldToResolutionPolicy), "
        "заменяет ручную mirror-репликацию audit_case3_hold_vs_tpsl_replay.py.",
        f"tpsl_pnl (baseline, факт as-is из execution_orders.net_pnl, сумма по портфелю) = {tpsl_pnl}",
        f"hold_to_resolution_pnl (variant, контрфактика, сумма по портфелю) = {hold_pnl}",
        f"delta_pct = (hold_pnl - tpsl_pnl) / |tpsl_pnl| * 100 = {delta_pct} "
        "— единственная величина 'сколько денег изменилось' в этом отчёте, "
        "считается из сумм (tpsl_pnl/hold_pnl), не из delta_pnl ниже.",
        f"delta_pnl (среднее per-trajectory variant-baseline, НЕ портфельная сумма "
        f"и НЕ то же самое, что delta_pct выше) = {combined.delta_pnl}",
        f"per-event bootstrap 95% CI: [{combined.ci_low}, {combined.ci_high}] — построен "
        "вокруг среднего per-trajectory эффекта (та же величина, что delta_pnl строкой "
        "выше), НЕ вокруг портфельной суммы/delta_pct — не трактовать эти границы как "
        "доверительный интервал для delta_pct.",
        "Ограничение (наследуется от audit_case3): только 2 детектора с покрытием "
        "снапшотами (D38_resolution_lag, D42_event_driven_ml), не обобщать на весь "
        "портфель. EARLY_EXIT/EXPIRED позиции используют LOSS-proxy — направление "
        "результата устойчиво, точная величина — нет.",
    ]

    return {
        "per_detector": per_detector,
        "metrics": {
            "hold_to_resolution_pnl": hold_pnl,
            "tpsl_pnl": tpsl_pnl,
            "delta_pct": delta_pct,
            "delta_pnl_mean_per_trajectory": combined.delta_pnl,
            "ci_low": combined.ci_low,
            "ci_high": combined.ci_high,
            "n_trades": combined.n,
            "per_detector": per_detector,
        },
        "verdict": "\n".join(verdict_lines),
    }


_DATASET_HASH_SQL = """
SELECT count(*) AS n, max(eo.filled_at) AS max_filled_at
  FROM execution_orders eo
  JOIN detector_signals ds ON ds.id = eo.detector_signal_id
 WHERE eo.status = 'virtual_settled' AND eo.execution_mode = 'virtual'
   AND eo.detector = ANY($1) AND eo.filled_at >= $2
   AND eo.outcome IN ('WIN', 'LOSS', 'EARLY_EXIT', 'EXPIRED')
"""


async def _dataset_hash(start_date: datetime) -> str:
    row = await fetch(_DATASET_HASH_SQL, list(DETECTORS_WITH_SNAPSHOT_COVERAGE), start_date)
    n = row[0]["n"] if row else 0
    max_filled_at = row[0]["max_filled_at"] if row else None
    return hashlib.sha256(f"{n}:{max_filled_at}".encode()).hexdigest()[:16]


def _config_hash(start_date: datetime) -> str:
    config = {
        "detectors": list(DETECTORS_WITH_SNAPSHOT_COVERAGE),
        "start_date": start_date.isoformat(),
        "bootstrap_resamples": 1500,
        "bootstrap_seed": 42,
        "early_exit_expired_proxy_outcome": "LOSS",
    }
    return hashlib.sha256(json.dumps(config, sort_keys=True).encode()).hexdigest()[:16]


_INSERT_RUN_SQL = """
INSERT INTO agent_runs (kind, name, config_hash, dataset_hash, query_version, metrics, verdict, finished_at)
VALUES ('audit', $1, $2, $3, $4, $5::jsonb, $6, now())
RETURNING id
"""

_CLAIM_STATEMENT = "hold-to-resolution лучше as-is TP/SL на собственных данных проекта (counterfactual engine)"

_UPSERT_CLAIM_SQL = """
INSERT INTO agent_claims (statement, scope, status, evidence_run_ids, verify_policy, verify_halflife_days)
VALUES ($1, 'policy:exit_strategy', 'active', ARRAY[$2]::bigint[], 'time_halflife', 30)
ON CONFLICT (statement) DO UPDATE
   SET evidence_run_ids = agent_claims.evidence_run_ids || EXCLUDED.evidence_run_ids,
       last_verified_at = now(),
       updated_at = now()
"""


async def persist(result: dict, start_date: datetime) -> int:
    """Записать agent_runs + upsert agent_claims. Требует write-права read-only
    роли oraclepoly_agent_ro на 4 agent_* таблицы (тот же грант, что audit_case3)."""
    config_hash = _config_hash(start_date)
    dataset_hash = await _dataset_hash(start_date)

    run_row = await fetch(
        _INSERT_RUN_SQL,
        _RUN_NAME,
        config_hash,
        dataset_hash,
        _QUERY_VERSION,
        json.dumps(result["metrics"]),
        result["verdict"],
    )
    run_id = run_row[0]["id"]

    await execute(_UPSERT_CLAIM_SQL, _CLAIM_STATEMENT, run_id)

    return run_id


async def _run(start_date: datetime, *, dry_run: bool) -> dict:
    settings = get_settings()
    dsn = settings.agent_database_url
    if not dsn:
        print(
            "WARNING: agent_database_url не задан — read-only роль oraclepoly_agent_ro "
            "ещё не сконфигурирована. Скрипт НЕ должен молча уходить под основным "
            "DATABASE_URL для read-only аудита.",
            file=sys.stderr,
        )
        sys.exit(1)

    await init_db(dsn=dsn)
    try:
        result = await run_audit(start_date)
        run_id = None
        if not dry_run:
            run_id = await persist(result, start_date)
        return {**result, "run_id": run_id}
    finally:
        await close_db()


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--start",
        default="2026-05-28",
        help="YYYY-MM-DD — окно совпадает с audit_case3 (снапшоты покрывают только с 28 мая 2026).",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Посчитать и напечатать метрики, но не писать в agent_runs/agent_claims.",
    )
    args = parser.parse_args()

    start_date = datetime.strptime(args.start, "%Y-%m-%d").replace(tzinfo=UTC)
    result = asyncio.run(_run(start_date, dry_run=args.dry_run))

    print(json.dumps(result, indent=2, default=str))


if __name__ == "__main__":
    main()
