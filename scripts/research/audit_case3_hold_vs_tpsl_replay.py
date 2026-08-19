"""Phase 0 Portfolio Analyst Agent — replay-кейс 3: hold-to-resolution vs TP/SL/trailing/partial.

Обёртка над методом `scripts/research/exit_strategy_backtest.py` (эталон, не менять),
которая пишет результат в `agent_runs`/`agent_claims` вместо (в дополнение к) markdown-отчёта.
Не пересчитывает метод заново — переиспользует те же функции симуляции
(`simulate_trailing_stop`, `simulate_time_decay`, `simulate_partial_exit`) и тот же
источник данных (`execution_orders` + `detector_signal_price_snapshots`), чтобы прогон
Phase 0 был методологически идентичен ручному аудиту 2026-07-03, который лёг в основу
memory `exit-strategies-hold-to-resolution-confirmed-2026-07-03` и последующих
PR #663/#670/#668 (см. "База сравнения" ниже).

Ключевая методологическая деталь, отсутствующая в эталонном скрипте (тот сравнивал
baseline=факт против trailing/decay/partial, но факт для D38/D42 — это УЖЕ
TP/SL-гейтированная as-is политика, не чистый hold-to-resolution — см.
app/jobs/position_manager.py:169-176,429-433: HOLD_TO_RESOLUTION_DETECTORS отключает
только mid_zone_exit, TP/SL остаются как circuit-breakers). Этот скрипт добавляет
четвёртую симуляцию — `simulate_hold_to_resolution` — которая игнорирует TP/SL/MZE
целиком и удерживает позицию до фактического исхода резолюции (WIN/LOSS из БД), тем
самым честно противопоставляя "как было в БД" (as-is TP/SL circuit-breaker policy)
против "если бы держали до конца" (чистый hold-to-resolution counterfactual) — именно
это сравнение и требует Task 6 brief (`hold_to_resolution_pnl` vs `tpsl_pnl`).

Метод (не look-ahead, воспроизводим):
  - Данные — уже сгенерированные реальные virtual-сигналы, НЕ пересчитываются задним
    числом (в отличие от canonical-бэктеста). Ценовая траектория берётся строго между
    фактическим filled_at и фактическим exit/resolution — будущее относительно точки
    входа никогда не используется для решения о выходе (chronological forward replay
    по снапшотам ASC).
  - "per-event bootstrap" в терминологии брифа здесь = один trade = одна позиция
    (execution_orders.id), не разбивается на суб-события — Sortino считается на
    полном списке net_pnl каждого варианта (та же формула `_sortino_ratio`, что в
    эталонном скрипте), не на псевдослучайной выборке (детерминированность важнее
    для agent_runs.metrics, чем CI из ресэмплинга, которого в эталоне тоже нет).
  - "mirror-вариант" = то же самое сравнение as-is TP/SL (факт) против ЧЕТЫРЁХ
    контрфактических альтернатив (hold-to-resolution, trailing stop, time-decay TP/SL,
    partial exit) на ОДНОМ И ТОМ ЖЕ наборе траекторий — не разные выборки на вариант.
  - Только детекторы с покрытием `detector_signal_price_snapshots`
    (D38_resolution_lag, D42_event_driven_ml, verified в эталонном скрипте
    2026-07-03) — остальные не имеют траектории цены между входом и выходом,
    на них выводы не распространяются (то же ограничение, что в оригинале).
  - Proxy-исход для EARLY_EXIT/EXPIRED позиций (нет реального WIN/LOSS в БД —
    рынок для EARLY_EXIT мог резолвиться позже другим способом, для EXPIRED
    резолюция не наступила в срок) — LOSS, тот же консервативный proxy, что
    `simulate_partial_exit` в эталонном скрипте уже использует для remainder-ноги.
    Направление результата устойчиво к этой оговорке, точная величина — нет
    (наследуется как явное ограничение из эталона, см. backtest-results.md
    "Ограничения и честные оговорки").

База сравнения для "+127%" (см. Task 6 Open risk в
docs/superpowers/plans/2026-07-20-portfolio-agent-phase0.md): точная цифра не найдена
дословно ни в одном из #626/#663/#670/#668 при ресёрче этой задачи — вероятный источник
это агрегированная разница между as-is TP/SL(fact) net_pnl и hold-to-resolution
net_pnl из последующего trajectory-audit (#663/#670), закреплённого как circuit-breaker
в position_manager.py (#673), а не из самого #626 (тот дал абсолютные суммы в $ по
детекторам раздельно, не единый %). delta_pct в этом прогоне определяется явно:
    delta_pct = (hold_to_resolution_pnl - tpsl_pnl) / abs(tpsl_pnl) * 100
именно чтобы будущие прогоны были сравнимы между собой по одной и той же формуле,
даже если абсолютное число разойдётся с историческим — допуск по Acceptance Task 6
широкий (направление + порядок величины, не точный процент).

Запускать ТОЛЬКО на проде, через read-only роль (Task 2, `oraclepoly_agent_ro`) — см.
`init_db(dsn=get_settings().agent_database_url)` ниже. Через -m (не прямой путь к
файлу — иначе Python кладёт директорию скрипта первой в sys.path и `from app...` падает):
    docker exec oraclepoly-backend python -m scripts.research.audit_case3_hold_vs_tpsl_replay \
        --start 2026-05-28

Superseded (2026-07-20): заменён scripts/research/counterfactual_case1_hold_vs_tpsl.py
(Task 4), который использует app/analytics/counterfactual.py вместо разовой
mirror-репликации. Этот файл НЕ удаляется (Phase 0 артефакт, история/трассировка).
simulate_hold_to_resolution по-прежнему импортируется отсюда напрямую в
app/analytics/policies/exit.py (не дублируется).
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import sys
from datetime import UTC, datetime

from app.core.config import get_settings
from app.jobs.v2_execution_resolve import _compute_settlement_pnl
from app.services.database import close_db, execute, fetch, init_db
from scripts.research.exit_strategy_backtest import (
    PositionTrajectory,
    SimResult,
    _sortino_ratio,
    fetch_resolved_with_snapshots,
    simulate_partial_exit,
    simulate_time_decay,
    simulate_trailing_stop,
)

# Верифицированное покрытие снапшотами (эталонный скрипт + backtest-results.md,
# 2026-07-03): только эти два детектора физически имеют ценовую траекторию между
# входом и выходом в окне с 28 мая 2026. Не расширять список без повторной проверки
# `detector_signal_price_snapshots` на проде — остальные ~20 детекторов её не имеют.
DETECTORS_WITH_SNAPSHOT_COVERAGE = ("D38_resolution_lag", "D42_event_driven_ml")

_RUN_NAME = "hold_vs_tpsl_replay"
_QUERY_VERSION = "audit_case3_hold_vs_tpsl_replay@1.0.0"


def simulate_hold_to_resolution(traj: PositionTrajectory, actual_outcome: str) -> SimResult:
    """Чистый hold-to-resolution counterfactual: игнорирует TP/SL/MZE целиком,
    держит позицию до фактического исхода резолюции (не до последнего снапшота —
    в отличие от `simulate_trailing_stop`/`simulate_time_decay`, которым при
    отсутствии триггера некуда деться кроме конца доступных данных).

    actual_outcome — WIN/LOSS/proxy-LOSS для EARLY_EXIT/EXPIRED (см. docstring модуля).
    Использует `_compute_settlement_pnl` (одна нога, resolution — не активная
    торговля, fee=0 при выигрыше) — НЕ `_compute_early_exit_pnl` (та для активного
    выхода из книги, двойная комиссия). Это и есть содержательная разница между
    hold-to-resolution и TP/SL/trailing/partial: у первого нет exit-ноги вообще.
    """
    pnl = _compute_settlement_pnl(
        stake=traj.stake,
        fill_price=traj.fill_price,
        outcome=actual_outcome,
        category=traj.category,
        fee_schedule=traj.fee_schedule,
    )
    return SimResult(
        order_id=traj.order_id,
        exit_reason="hold_to_resolution",
        exit_price=float("nan"),  # нет единой exit-цены — резолюция, не торговля
        net_pnl=pnl["net_pnl"],
    )


def _pnl_total(pnls: list[float]) -> float:
    return round(sum(pnls), 2)


async def _simulate_all(trajectories: list[PositionTrajectory]) -> dict:
    """Прогнать as-is baseline (факт из БД, TP/SL-гейтированная политика) + 4
    контрфактики (hold-to-resolution, trailing, decay, partial) на одном наборе
    траекторий — mirror-сравнение, не разные выборки на каждый вариант."""
    tpsl_pnls: list[float] = []  # факт из execution_orders.net_pnl (as-is TP/SL circuit-breaker)
    hold_pnls: list[float] = []
    trailing_pnls: list[float] = []
    decay_pnls: list[float] = []
    partial_pnls: list[float] = []

    for traj in trajectories:
        row = await fetch(
            "SELECT net_pnl::float AS net_pnl, outcome FROM execution_orders WHERE id = $1", traj.order_id
        )
        actual_outcome = "LOSS"
        if row:
            tpsl_pnls.append(row[0]["net_pnl"] or 0.0)
            actual_outcome = row[0]["outcome"]

        # Proxy-исход: EARLY_EXIT/EXPIRED не имеют реального WIN/LOSS в БД (см. docstring
        # модуля) — тот же консервативный LOSS-proxy, что simulate_partial_exit в эталоне.
        proxy_outcome = actual_outcome if actual_outcome in ("WIN", "LOSS") else "LOSS"

        hold_result = simulate_hold_to_resolution(traj, proxy_outcome)
        hold_pnls.append(hold_result.net_pnl)

        trail_result = simulate_trailing_stop(traj, trail_pct=15.0)
        if trail_result:
            trailing_pnls.append(trail_result.net_pnl)

        decay_result = simulate_time_decay(
            traj, wide_sl_pct=-30.0, narrow_sl_pct=-10.0, wide_tp_pct=40.0, narrow_tp_pct=15.0
        )
        if decay_result:
            decay_pnls.append(decay_result.net_pnl)

        partial_result = simulate_partial_exit(
            traj, partial_trigger_pct=15.0, partial_fraction=0.5, actual_outcome=proxy_outcome
        )
        if partial_result:
            partial_pnls.append(partial_result.net_pnl)

    return {
        "tpsl": tpsl_pnls,
        "hold": hold_pnls,
        "trailing": trailing_pnls,
        "decay": decay_pnls,
        "partial": partial_pnls,
    }


def _delta_pct(hold_pnl: float, tpsl_pnl: float) -> float | None:
    """(hold - tpsl) / |tpsl| * 100 — положительно, если hold-to-resolution лучше
    фактической as-is TP/SL-политики. Формула зафиксирована явно (см. docstring
    модуля "База сравнения") ради сравнимости прогонов между собой, независимо от
    того, совпадёт ли абсолютная величина с исторической "+127%"."""
    if tpsl_pnl == 0:
        return None
    return round((hold_pnl - tpsl_pnl) / abs(tpsl_pnl) * 100.0, 2)


async def run_audit(start_date: datetime) -> dict:
    """Прогнать реплей по обоим верифицированным детекторам, объединить в единый
    портфельный агрегат (не только per-detector, как в эталонном скрипте) —
    Phase 0 audit сравнивает hold-to-resolution vs as-is TP/SL на уровне всего
    покрытого снапшотами набора позиций, per-detector разбивка остаётся в metrics."""
    per_detector: dict[str, dict] = {}
    combined_tpsl: list[float] = []
    combined_hold: list[float] = []
    combined_trailing: list[float] = []
    combined_decay: list[float] = []
    combined_partial: list[float] = []

    for detector in DETECTORS_WITH_SNAPSHOT_COVERAGE:
        trajectories = await fetch_resolved_with_snapshots(detector, start_date)
        pnls = await _simulate_all(trajectories)

        combined_tpsl.extend(pnls["tpsl"])
        combined_hold.extend(pnls["hold"])
        combined_trailing.extend(pnls["trailing"])
        combined_decay.extend(pnls["decay"])
        combined_partial.extend(pnls["partial"])

        per_detector[detector] = {
            "n_trajectories": len(trajectories),
            "tpsl_pnl": _pnl_total(pnls["tpsl"]),
            "hold_to_resolution_pnl": _pnl_total(pnls["hold"]),
            "trailing_stop_pnl": _pnl_total(pnls["trailing"]),
            "time_decay_pnl": _pnl_total(pnls["decay"]),
            "partial_exit_pnl": _pnl_total(pnls["partial"]),
            "tpsl_sortino": _sortino_ratio(pnls["tpsl"]),
            "hold_sortino": _sortino_ratio(pnls["hold"]),
        }

    tpsl_pnl = _pnl_total(combined_tpsl)
    hold_pnl = _pnl_total(combined_hold)
    trailing_pnl = _pnl_total(combined_trailing)
    decay_pnl = _pnl_total(combined_decay)
    partial_pnl = _pnl_total(combined_partial)
    delta_pct = _delta_pct(hold_pnl, tpsl_pnl)
    n_trades = len(combined_tpsl)

    alternatives_worse_or_equal = (
        trailing_pnl <= hold_pnl and decay_pnl <= hold_pnl and partial_pnl <= hold_pnl
    )

    verdict_lines = [
        "Метод: mirror-replay поверх scripts/research/exit_strategy_backtest.py "
        "(per-detector траектории D38_resolution_lag/D42_event_driven_ml, "
        "chronological forward simulation, без look-ahead; hold-to-resolution — новая "
        "симуляция simulate_hold_to_resolution, отсутствовавшая в эталоне).",
        f"tpsl_pnl (факт as-is из execution_orders.net_pnl, TP/SL-гейтированная политика) = {tpsl_pnl}",
        f"hold_to_resolution_pnl (контрфактика: игнорирует TP/SL/MZE, держит до резолюции) = {hold_pnl}",
        f"trailing_stop_15pct_pnl (контрфактика) = {trailing_pnl}",
        f"time_decay_pnl (контрфактика мягкого TP/SL) = {decay_pnl}",
        f"partial_exit_50pct_pnl (контрфактика) = {partial_pnl}",
        f"delta_pct = (hold - tpsl) / |tpsl| * 100 = {delta_pct}",
        "База сравнения для исторической '+127%': точная цифра не найдена дословно "
        "в PR #626/#663/#670/#668 при ресёрче этого прогона — наиболее вероятный "
        "источник #663/#670 (trajectory/MAE-MFE audit), закреплённый как circuit-breaker "
        "в #673 (position_manager.py), НЕ #626 напрямую (тот дал абсолютные $ по "
        "детекторам раздельно, не единый %). Допуск по Task 6 Acceptance — направление "
        "и порядок величины, не точное число.",
        (
            "Trailing/time-decay/partial-exit все хуже (или не лучше) hold-to-resolution "
            "на этом наборе позиций — согласуется с memory "
            "exit-strategies-hold-to-resolution-confirmed-2026-07-03."
            if alternatives_worse_or_equal
            else "ВНИМАНИЕ: минимум одна альтернатива (trailing/decay/partial) превзошла "
            "hold-to-resolution на этом прогоне — расхождение с memory "
            "exit-strategies-hold-to-resolution-confirmed-2026-07-03, требует ручного разбора "
            "перед тем как считать клейм подтверждённым (напр. известный кейс time-decay на "
            "D42 — единственное исключение в оригинальном 2026-07-03 аудите)."
        ),
        "Ограничение (наследуется от эталона): только 2 детектора с покрытием снапшотами "
        "(D38_resolution_lag, D42_event_driven_ml), не обобщать на весь портфель. "
        "EARLY_EXIT/EXPIRED позиции используют LOSS-proxy для остатка/hold-исхода — "
        "направление результата устойчиво, точная величина — нет.",
    ]
    verdict = "\n".join(verdict_lines)

    return {
        "per_detector": per_detector,
        "metrics": {
            "hold_to_resolution_pnl": hold_pnl,
            "tpsl_pnl": tpsl_pnl,
            "trailing_stop_pnl": trailing_pnl,
            "time_decay_pnl": decay_pnl,
            "partial_exit_pnl": partial_pnl,
            "delta_pct": delta_pct,
            "n_trades": n_trades,
            "per_detector": per_detector,
        },
        "verdict": verdict,
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
        "trailing_trail_pct": 15.0,
        "decay_wide_sl_pct": -30.0,
        "decay_narrow_sl_pct": -10.0,
        "decay_wide_tp_pct": 40.0,
        "decay_narrow_tp_pct": 15.0,
        "partial_trigger_pct": 15.0,
        "partial_fraction": 0.5,
        "early_exit_expired_proxy_outcome": "LOSS",
    }
    return hashlib.sha256(json.dumps(config, sort_keys=True).encode()).hexdigest()[:16]


_INSERT_RUN_SQL = """
INSERT INTO agent_runs (kind, name, config_hash, dataset_hash, query_version, metrics, verdict, finished_at)
VALUES ('audit', $1, $2, $3, $4, $5::jsonb, $6, now())
RETURNING id
"""

_CLAIM_STATEMENT = "hold-to-resolution лучше TP/SL/trailing/partial exit на собственных данных проекта"

_UPSERT_CLAIM_SQL = """
INSERT INTO agent_claims (statement, scope, status, evidence_run_ids, verify_policy, verify_halflife_days)
VALUES ($1, 'policy:exit_strategy', 'active', ARRAY[$2]::bigint[], 'time_halflife', 30)
ON CONFLICT (statement) DO UPDATE
   SET evidence_run_ids = agent_claims.evidence_run_ids || EXCLUDED.evidence_run_ids,
       last_verified_at = now(),
       updated_at = now()
"""


async def persist(result: dict, start_date: datetime) -> int:
    """Записать agent_runs + upsert agent_claims. Требует write-права на 4 agent_*
    таблицы — выданы read-only роли `oraclepoly_agent_ro` точечным GRANT (Task 2:
    SELECT/INSERT/UPDATE на agent_claims/agent_runs/agent_actions/agent_llm_calls),
    несмотря на общее название "read-only" (она read-only только на торговые таблицы)."""
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
            "ещё не сконфигурирована (Task 2). Скрипт НЕ должен молча уходить под "
            "основным DATABASE_URL для read-only аудита.",
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
        help="YYYY-MM-DD — окно совпадает с эталонным аудитом 2026-07-03 "
        "(снапшоты покрывают только с 28 мая 2026, раньше данных физически нет).",
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
