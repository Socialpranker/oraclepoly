"""Phase 0 replay-кейс 1: sum-ROI vs портфельный ROI в demote-алерте D42.

Портфель Analyst Agent, Phase 0 (docs/specs/portfolio_analyst_agent.md §8) —
детерминированный аудит, воспроизводящий на исторических данных баг
2026-07-16: demote-алерт D42 сообщил "sum_roi_net = -1058%" — это СУММА
per-signal `realized_roi_net` (уже в процентах) по терминальным сигналам,
а не доходность на вложенный капитал (см. app/jobs/demote_alerts_monitor.py:146,161
и app/services/detector_metrics.py:161-208 _SHADOW_SIGNALS_7D_SQL — источник
и бага, и правильного mean_roi_net_7d в одном SQL-хелпере).

Источник данных: detector_signals (НЕ execution_orders) — realized_roi_net
живёт на detector_signals, а не на execution_orders (verified read-only на
проде 2026-07-20: execution_orders не имеет колонки realized_roi_net).
_SHADOW_SIGNALS_7D_SQL использует `generated_at > now() - INTERVAL '7 days'`
без параметризации по историческому as_of, поэтому переиспользовать его
напрямую для replay на дату 2026-07-16 нельзя — здесь собственный запрос
с фиксированным as_of.

Bankroll-weighted вариант (в) считается отдельно с execution_orders
(net_pnl/stake — единственные пригодные колонки для notional на этой
таблице; bankroll_at_entry — размер банка на момент сделки, не notional
одной ставки) — так как detector_signals не хранит net_pnl/stake напрямую.

Запускать ТОЛЬКО на проде (read-only agent-роль, см. CLAUDE.md
"scripts/research/*.py — one-off скрипты для VPS"):
    docker exec oraclepoly-backend python scripts/research/audit_case1_d42_portfolio_roi_replay.py

До применения Task 1 (миграция agent_runs/agent_claims) и Task 2
(read-only роль oraclepoly_agent_ro + AGENT_DATABASE_URL пароль) на проде
скрипт не подключится — init_db(dsn=settings.agent_database_url) упадёт,
т.к. роли ещё не существует. Ожидаемо, не баг этого скрипта.
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

# Allow execution from repo root: PYTHONPATH=.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))

from app.core.config import get_settings  # noqa: E402
from app.services.database import close_db, execute, fetch, fetchrow, init_db  # noqa: E402

DETECTOR = "D42_event_driven_ml"
DEFAULT_AS_OF = "2026-07-16 12:00:00+00"
WINDOW_DAYS = 7

# ─── Step 1/2: sum vs mean per-signal ROI за 7d окно, фиксированный as_of ────
#
# Источник sum_roi_net/mean_roi_net в demote-алерте: detector_signals,
# terminal = outcome IN ('WIN','LOSS'), окно generated_at > as_of - 7d.
# Идентично агрегации _SHADOW_SIGNALS_7D_SQL (detector_metrics.py:161-208),
# но с параметризуемым as_of вместо now().
_SIGNAL_ROI_7D_SQL = """
SELECT
    COUNT(*) FILTER (WHERE ds.outcome IN ('WIN', 'LOSS')) AS n_terminal,
    COUNT(*) FILTER (WHERE ds.outcome = 'WIN') AS wins,
    COALESCE(
        SUM(ds.realized_roi_net) FILTER (WHERE ds.outcome IN ('WIN', 'LOSS')),
        0
    )::float AS sum_roi_pct,
    AVG(ds.realized_roi_net) FILTER (WHERE ds.outcome IN ('WIN', 'LOSS'))::float
        AS mean_roi_pct,
    COUNT(*) AS total_signals,
    MAX(ds.created_at) AS max_created_at
FROM detector_signals ds
WHERE ds.detector = $1
  AND ds.generated_at > $2::timestamptz - make_interval(days => $3)
  AND ds.generated_at <= $2::timestamptz
"""

# ─── Step 2(в): bankroll-weighted ROI — execution_orders, notional = stake ───
#
# detector_signals не хранит net_pnl/stake — bankroll-weighted вариант
# считается на execution_orders отдельно (open risk из брифа): notional
# unit = stake (единственная пригодная колонка на execution_orders; см.
# docstring модуля). bankroll_at_entry — размер банка на момент входа,
# НЕ notional одной ставки, поэтому не используется как делитель.
_BANKROLL_WEIGHTED_SQL = """
SELECT
    COUNT(*) AS n_orders,
    COALESCE(SUM(eo.net_pnl), 0)::float AS sum_net_pnl,
    COALESCE(SUM(eo.stake), 0)::float AS sum_stake
FROM execution_orders eo
WHERE eo.detector = $1
  AND eo.status IN ('virtual_settled', 'settled')
  AND eo.created_at > $2::timestamptz - make_interval(days => $3)
  AND eo.created_at <= $2::timestamptz
"""

# ─── Step 3: месячные бакеты — деградация июнь→июль, БЕЗ 7d-окна ────────────
#
# 7-дневное окно (как в живом алерте) целиком попадает в один месяц —
# деградацию между месяцами оно структурно не может показать. Для этого
# кейса нужен более широкий срез: вся история detector_signals для D42 до
# as_of, агрегированная по date_trunc('month', generated_at). Это
# сознательно другое окно, чем Step 1/2 (7d) — не подмена, а
# дополнительный разрез, который и просит Step 3 брифа.
_MONTHLY_BUCKETS_SQL = """
SELECT
    date_trunc('month', ds.generated_at) AS month,
    COUNT(*) FILTER (WHERE ds.outcome IN ('WIN', 'LOSS')) AS n_terminal,
    COALESCE(
        SUM(ds.realized_roi_net) FILTER (WHERE ds.outcome IN ('WIN', 'LOSS')),
        0
    )::float AS sum_roi_pct,
    AVG(ds.realized_roi_net) FILTER (WHERE ds.outcome IN ('WIN', 'LOSS'))::float
        AS mean_roi_pct
FROM detector_signals ds
WHERE ds.detector = $1
  AND ds.generated_at <= $2::timestamptz
GROUP BY 1
ORDER BY 1
"""

# ─── Step 4: hold-to-resolution counterfactual — execution_orders.exit_reason ─
#
# exit_reason колонка verified read-only на проде 2026-07-20 (\\d execution_orders):
# TEXT, nullable, наблюдаемые значения для D42 = 'resolution' | 'stop_loss' |
# 'take_profit' | 'mid_zone_exit'. hold-to-resolution counterfactual = все
# строки КРОМЕ stop_loss-выходов (early exit по стоп-лоссу — то, что
# подозревается как источник убытка, ср. память virtual-stoploss-is-the-loss).
_HOLD_TO_RESOLUTION_SQL = """
SELECT
    eo.exit_reason,
    COUNT(*) AS n_orders,
    COALESCE(SUM(eo.net_pnl), 0)::float AS sum_net_pnl,
    COALESCE(SUM(eo.stake), 0)::float AS sum_stake
FROM execution_orders eo
WHERE eo.detector = $1
  AND eo.status IN ('virtual_settled', 'settled')
  AND eo.created_at > $2::timestamptz - make_interval(days => $3)
  AND eo.created_at <= $2::timestamptz
  AND (eo.exit_reason IS NULL OR eo.exit_reason != 'stop_loss')
GROUP BY eo.exit_reason
ORDER BY eo.exit_reason NULLS FIRST
"""

_ALL_EXITS_SQL = """
SELECT
    eo.exit_reason,
    COUNT(*) AS n_orders,
    COALESCE(SUM(eo.net_pnl), 0)::float AS sum_net_pnl,
    COALESCE(SUM(eo.stake), 0)::float AS sum_stake
FROM execution_orders eo
WHERE eo.detector = $1
  AND eo.status IN ('virtual_settled', 'settled')
  AND eo.created_at > $2::timestamptz - make_interval(days => $3)
  AND eo.created_at <= $2::timestamptz
GROUP BY eo.exit_reason
ORDER BY eo.exit_reason NULLS FIRST
"""


def _config_hash(as_of: datetime, window_days: int) -> str:
    """sha256(SQL текста + as_of + window) — воспроизводимость без LLM."""
    payload = "\n---\n".join(
        [
            _SIGNAL_ROI_7D_SQL,
            _BANKROLL_WEIGHTED_SQL,
            _MONTHLY_BUCKETS_SQL,
            _HOLD_TO_RESOLUTION_SQL,
            _ALL_EXITS_SQL,
            f"as_of={as_of}",
            f"window_days={window_days}",
            f"detector={DETECTOR}",
        ]
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _dataset_hash(row_count: int, max_created_at: datetime | None) -> str:
    """sha256(f"{row_count}:{max_created_at}") — снапшот исходных данных."""
    payload = f"{row_count}:{max_created_at.isoformat() if max_created_at else 'null'}"
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


async def compute_signal_roi(as_of: datetime) -> dict[str, Any]:
    """Step 1/2(а,б): sum_roi_pct (баг) vs mean per-signal ROI (портфельный кандидат #1)."""
    row = await fetchrow(_SIGNAL_ROI_7D_SQL, DETECTOR, as_of, WINDOW_DAYS)
    return dict(row) if row else {}


async def compute_bankroll_weighted(as_of: datetime) -> dict[str, Any] | None:
    """Step 2(в): bankroll-weighted ROI = SUM(net_pnl) / SUM(stake) на execution_orders.

    Возвращает None (явное ограничение, не гадаем) если sum_stake == 0 —
    нет settled-ордеров с notional в окне.
    """
    row = await fetchrow(_BANKROLL_WEIGHTED_SQL, DETECTOR, as_of, WINDOW_DAYS)
    if not row or not row["sum_stake"]:
        return None
    roi_pct = (row["sum_net_pnl"] / row["sum_stake"]) * 100.0
    return {
        "roi_pct": roi_pct,
        "n_orders": row["n_orders"],
        "sum_net_pnl": row["sum_net_pnl"],
        "sum_stake": row["sum_stake"],
    }


async def compute_monthly_buckets(as_of: datetime) -> list[dict[str, Any]]:
    """Step 3: месячные бакеты (полная история до as_of, без 7d-окна) — июнь vs июль."""
    rows = await fetch(_MONTHLY_BUCKETS_SQL, DETECTOR, as_of)
    return [dict(r) for r in rows]


async def compute_hold_to_resolution_counterfactual(as_of: datetime) -> dict[str, Any]:
    """Step 4: hold-to-resolution counterfactual (exclude exit_reason='stop_loss') vs все exits.

    Подтверждает/опровергает, что stop_loss exit целиком делает 7d-убыток.
    """
    hold_rows = await fetch(_HOLD_TO_RESOLUTION_SQL, DETECTOR, as_of, WINDOW_DAYS)
    all_rows = await fetch(_ALL_EXITS_SQL, DETECTOR, as_of, WINDOW_DAYS)

    def _summarize(rows: list[Any]) -> dict[str, Any]:
        n_orders = sum(r["n_orders"] for r in rows)
        sum_net_pnl = sum(r["sum_net_pnl"] for r in rows)
        sum_stake = sum(r["sum_stake"] for r in rows)
        roi_pct = (sum_net_pnl / sum_stake * 100.0) if sum_stake else None
        return {
            "n_orders": n_orders,
            "sum_net_pnl": sum_net_pnl,
            "sum_stake": sum_stake,
            "roi_pct_bankroll_weighted": roi_pct,
        }

    return {
        "hold_to_resolution_excl_stop_loss": _summarize(hold_rows),
        "all_exits": _summarize(all_rows),
        "by_exit_reason": [dict(r) for r in all_rows],
    }


def _build_verdict(
    signal_roi: dict[str, Any],
    bankroll_weighted: dict[str, Any] | None,
    monthly: list[dict[str, Any]],
    hold_to_resolution: dict[str, Any],
) -> str:
    """Свободный текст (Phase 0 ещё не использует фиксированный словарь §4.2)."""
    lines = [
        f"sum_roi_pct (баг-репродукция) = {signal_roi.get('sum_roi_pct'):.1f}% "
        f"на n_terminal={signal_roi.get('n_terminal')} — это СУММА per-signal realized_roi_net, "
        "не портфельный ROI. Оригинальный алерт 2026-07-16 03:25 UTC (demote_alert_log id=352) "
        "сообщил sum_roi_net=-1058.1% на n_terminal=157.",
        f"portfolio_roi_pct_mean (mean per-signal, кандидат #1) = {signal_roi.get('mean_roi_pct'):.2f}%.",
    ]
    if bankroll_weighted is not None:
        lines.append(
            f"portfolio_roi_pct_bankroll_weighted (SUM(net_pnl)/SUM(stake), кандидат #2) = "
            f"{bankroll_weighted['roi_pct']:.2f}% на n_orders={bankroll_weighted['n_orders']} "
            f"(execution_orders, notional=stake — bankroll_at_entry на этой таблице "
            "означает размер банка на момент входа, не notional ставки)."
        )
    else:
        lines.append(
            "portfolio_roi_pct_bankroll_weighted: НЕ ПОСЧИТАН — sum_stake=0 в окне "
            "(явное ограничение отчёта, не гадаем)."
        )
    lines.append(
        "Открытый вопрос метода: sum_roi_pct считается на detector_signals (7d окно от as_of), "
        "bankroll-weighted — на execution_orders (другая таблица, другая популяция строк — "
        "signal-level vs order-level, часть сигналов rejected/не исполнена). Прямое сравнение "
        "трёх чисел ограничено разной популяцией знаменателя; расхождение — метод, не баг."
    )
    if len(monthly) >= 2:
        m0, m1 = monthly[-2], monthly[-1]
        degraded = (m1.get("mean_roi_pct") or 0) < (m0.get("mean_roi_pct") or 0)
        lines.append(
            f"Месячная деградация ({m0['month'].strftime('%Y-%m')} → {m1['month'].strftime('%Y-%m')}): "
            f"mean_roi_pct {m0.get('mean_roi_pct'):.2f}% → {m1.get('mean_roi_pct'):.2f}% "
            f"({'ХУЖЕ' if degraded else 'не хуже'}, n_terminal {m0['n_terminal']}→{m1['n_terminal']})."
        )
    else:
        lines.append(
            f"Месячных бакетов < 2 ({len(monthly)}) — деградацию июнь→июль подтвердить/опровергнуть нельзя "
            "на доступных данных."
        )
    hold = hold_to_resolution["hold_to_resolution_excl_stop_loss"]
    allx = hold_to_resolution["all_exits"]
    if allx["sum_net_pnl"] is not None and hold["sum_net_pnl"] is not None:
        stop_loss_share = (
            (allx["sum_net_pnl"] - hold["sum_net_pnl"]) if allx["n_orders"] > hold["n_orders"] else None
        )
        lines.append(
            f"Hold-to-resolution counterfactual (excl. stop_loss): net_pnl={hold['sum_net_pnl']:.2f} "
            f"на n_orders={hold['n_orders']} vs все exits net_pnl={allx['sum_net_pnl']:.2f} "
            f"на n_orders={allx['n_orders']}."
            + (
                f" stop_loss-exits дали net_pnl={stop_loss_share:.2f} — "
                + ("ЦЕЛИКОМ делают убыток" if stop_loss_share is not None and stop_loss_share < 0 else "не единственный убыточный класс")
                if stop_loss_share is not None
                else ""
            )
        )
    return "\n".join(lines)


async def run(as_of: datetime, *, dry_run: bool) -> dict[str, Any]:
    signal_roi = await compute_signal_roi(as_of)
    bankroll_weighted = await compute_bankroll_weighted(as_of)
    monthly = await compute_monthly_buckets(as_of)
    hold_to_resolution = await compute_hold_to_resolution_counterfactual(as_of)

    n_terminal = int(signal_roi.get("n_terminal") or 0)
    wins = int(signal_roi.get("wins") or 0)
    wr = (wins / n_terminal) if n_terminal else None

    metrics: dict[str, Any] = {
        "sum_roi_pct": signal_roi.get("sum_roi_pct"),
        "portfolio_roi_pct_mean": signal_roi.get("mean_roi_pct"),
        "portfolio_roi_pct_bankroll_weighted": (bankroll_weighted["roi_pct"] if bankroll_weighted else None),
        "wr": wr,
        "n_terminal": n_terminal,
        "as_of": as_of,
        "window_days": WINDOW_DAYS,
        "monthly": {
            row["month"].strftime("%Y-%m"): {
                "n_terminal": row["n_terminal"],
                "sum_roi_pct": row["sum_roi_pct"],
                "mean_roi_pct": row["mean_roi_pct"],
            }
            for row in monthly
        },
        "hold_to_resolution_counterfactual": hold_to_resolution,
        "bankroll_weighted_detail": bankroll_weighted,
    }

    verdict = _build_verdict(signal_roi, bankroll_weighted, monthly, hold_to_resolution)

    row_count = int(signal_roi.get("total_signals") or 0)
    max_created_at = signal_roi.get("max_created_at")
    config_hash = _config_hash(as_of, WINDOW_DAYS)
    dataset_hash = _dataset_hash(row_count, max_created_at)

    print("=== D42 Portfolio ROI Replay — Phase 0 audit case 1 ===")
    print(f"as_of={as_of} window_days={WINDOW_DAYS} detector={DETECTOR}")
    print(json.dumps(metrics, indent=2, default=str))
    print("\n--- verdict ---")
    print(verdict)

    if dry_run:
        print("\n[DRY] Skipping agent_runs/agent_claims writes.")
        return {"metrics": metrics, "verdict": verdict, "run_id": None}

    # Step 5: записать agent_runs (immutable run record).
    run_row = await fetchrow(
        """
        INSERT INTO agent_runs (kind, name, config_hash, dataset_hash, query_version, metrics, verdict, started_at, finished_at)
        VALUES ($1, $2, $3, $4, $5, $6::jsonb, $7, now(), now())
        RETURNING id
        """,
        "audit",
        "d42_portfolio_roi_replay",
        config_hash,
        dataset_hash,
        "v1",
        json.dumps(metrics, default=str),
        verdict,
    )
    run_id = int(run_row["id"])
    print(f"\nagent_runs.id={run_id}")

    # Step 6: emit claim(s) — race-safe ON CONFLICT (statement), не SELECT-then-INSERT.
    claim_statement = (
        "sum_roi_net в demote-алерте D42 — сумма per-signal ROI (-1058% на 98 сигналах), "
        "не портфельный ROI"
    )
    await execute(
        """
        INSERT INTO agent_claims (statement, scope, evidence_run_ids, verify_policy, verify_halflife_days)
        VALUES ($1, $2, $3::bigint[], $4, $5)
        ON CONFLICT (statement) DO UPDATE
        SET evidence_run_ids = agent_claims.evidence_run_ids || EXCLUDED.evidence_run_ids,
            updated_at = now()
        """,
        claim_statement,
        "detector:d42",
        [run_id],
        "time_halflife",
        90,
    )
    print(f"agent_claims upserted: {claim_statement!r}")

    return {"metrics": metrics, "verdict": verdict, "run_id": run_id}


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--as-of",
        default=DEFAULT_AS_OF,
        help=f"Историческая точка отсчёта 7d-окна (default: {DEFAULT_AS_OF}) — не now(), для воспроизводимости.",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Посчитать и распечатать метрики, но не писать в agent_runs/agent_claims.",
    )
    return parser.parse_args()


async def main() -> None:
    args = _parse_args()
    # asyncpg требует datetime.datetime для timestamptz-параметров, не str —
    # парсим один раз здесь и передаём datetime дальше по пайплайну.
    as_of = datetime.fromisoformat(args.as_of.replace("Z", "+00:00")).astimezone(UTC)

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
        await run(as_of, dry_run=args.dry_run)
    finally:
        await close_db()


if __name__ == "__main__":
    asyncio.run(main())
