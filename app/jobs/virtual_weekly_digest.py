"""Virtual weekly digest — cron (weekly, Sunday), сводка WIN/LOSS/EXIT/EXPIRED по virtual-ордерам.

Virtual (execution_mode='virtual') — бэктест-режим бота, per-trade TG-уведомление
было бы слишком шумным (десятки/сотни в день). Вместо этого — одна сводка раз в
неделю за последние 7 дней. Live-позиции уведомляются per-trade сразу при settle,
см. app/jobs/v2_execution_resolve.py::_notify_settlement.

Разбивка по категориям кластеризована по событию (markets.event_id): бот входит
в один рынок десятками коррелированных ордеров, per-order агрегат врёт (аудит
2026-07-07 — весь плюс Crypto делали 2 BTC-рынка). ⚠️-флаг = P&L категории
сделан одним событием, а не категорией.
"""

from __future__ import annotations

from typing import Any

import structlog

from app.core.task_queue import debounce_acquire
from app.services.database import fetch

logger = structlog.get_logger(__name__)

_VIRTUAL_WEEKLY_SQL = """
SELECT outcome, COUNT(*) AS n, COALESCE(SUM(net_pnl), 0) AS net_pnl_sum
  FROM execution_orders
 WHERE execution_mode = 'virtual'
   AND status = 'virtual_settled'
   AND outcome IN ('WIN', 'LOSS', 'EARLY_EXIT', 'EXPIRED')
   AND settled_at > now() - interval '7 days'
 GROUP BY outcome
"""

# Кластер = событие: суммируем ордера события, потом агрегируем по категориям.
# EXPIRED (PnL=0 by design) не входит в знаменатель ROI.
_CATEGORY_WEEKLY_SQL = """
WITH ev AS (
    SELECT COALESCE(m.category, 'other') AS category,
           m.event_id,
           COALESCE(SUM(eo.net_pnl), 0) AS net_pnl,
           SUM(eo.stake) FILTER (WHERE eo.outcome <> 'EXPIRED') AS staked
      FROM execution_orders eo
      JOIN markets m ON m.polymarket_id = eo.market_id
     WHERE eo.execution_mode = 'virtual'
       AND eo.status = 'virtual_settled'
       AND eo.outcome IN ('WIN', 'LOSS', 'EARLY_EXIT', 'EXPIRED')
       AND eo.settled_at > now() - interval '7 days'
     GROUP BY 1, 2
)
SELECT category,
       COUNT(*) AS events,
       COALESCE(SUM(net_pnl), 0) AS net_pnl,
       COALESCE(SUM(net_pnl) / NULLIF(SUM(staked), 0), 0) AS roi,
       MAX(net_pnl) AS best_event_pnl,
       MIN(net_pnl) AS worst_event_pnl
  FROM ev
 GROUP BY category
 ORDER BY SUM(net_pnl) DESC
"""


# Денежная контрфактика shadow-гейта категорий (app/jobs/category_health.py):
# что случилось с ордерами сигналов, которые гейт БЫ срезал. net_pnl < 0 →
# гейт сэкономил бы деньги. DISTINCT ON — сигнал может быть зафлаган повторно
# (retry необработанного сигнала), считаем один раз.
_GATE_VERDICT_SQL = """
WITH flagged AS (
    SELECT DISTINCT ON (detector_signal_id) detector, gate, detector_signal_id
      FROM gate_shadow_log
     WHERE gate LIKE 'category_health:%'
     ORDER BY detector_signal_id, logged_at
)
SELECT f.detector,
       replace(f.gate, 'category_health:', '') AS category,
       COUNT(DISTINCT f.detector_signal_id) AS flagged,
       COUNT(*) FILTER (WHERE eo.status IN ('settled', 'virtual_settled')) AS settled,
       COALESCE(SUM(eo.net_pnl) FILTER (WHERE eo.status IN ('settled', 'virtual_settled')), 0) AS net_pnl
  FROM flagged f
  LEFT JOIN execution_orders eo ON eo.detector_signal_id = f.detector_signal_id
       AND eo.status NOT IN ('rejected', 'cancelled', 'virtual_cancelled')
 GROUP BY 1, 2
 ORDER BY net_pnl
"""

# Тренд: event-кластеризованный ROI ячейки detector×category, 30д vs предыдущие 30д.
# Алертим только смену знака при достаточном n в обоих окнах — детектор
# затухающего edge (all-time агрегаты маскируют, см. D14/D29).
_TREND_SQL = """
SELECT detector, category,
       COUNT(*) FILTER (WHERE last_settled_at >  now() - interval '30 days') AS ev_recent,
       COUNT(*) FILTER (WHERE last_settled_at <= now() - interval '30 days') AS ev_prior,
       COALESCE(SUM(net_pnl) FILTER (WHERE last_settled_at >  now() - interval '30 days'), 0)
           / NULLIF(SUM(staked) FILTER (WHERE last_settled_at >  now() - interval '30 days'), 0) AS roi_recent,
       COALESCE(SUM(net_pnl) FILTER (WHERE last_settled_at <= now() - interval '30 days'), 0)
           / NULLIF(SUM(staked) FILTER (WHERE last_settled_at <= now() - interval '30 days'), 0) AS roi_prior
  FROM event_performance
 WHERE mode = 'virtual'
   AND last_settled_at > now() - interval '60 days'
 GROUP BY 1, 2
"""

_TREND_MIN_EVENTS = 10

# Hold-to-resolution counterfactual: ранние выходы (TP/SL/MZE/max_hold = EARLY_EXIT)
# и таймауты (EXPIRED, as-is PnL=0) на РЕЗОЛВНУТЫХ рынках — что дал бы hold до
# резолюции вместо exit-политики. as-is ROI меряет exit-политику пополам с edge
# сигнала (exit-trajectories audit 2026-07-06: hold подтверждён везде, SL вреден).
_HOLD_COUNTERFACTUAL_SQL = """
SELECT eo.side, eo.stake, eo.fill_price, eo.net_pnl,
       COALESCE(m.category, 'other') AS category,
       m.event_id, m.fee_schedule, m.winner
  FROM execution_orders eo
  JOIN markets m ON m.polymarket_id = eo.market_id
 WHERE eo.execution_mode = 'virtual'
   AND eo.status = 'virtual_settled'
   AND eo.outcome IN ('EARLY_EXIT', 'EXPIRED')
   AND eo.settled_at > now() - interval '60 days'
   AND m.resolved = true
   AND m.winner IN ('yes', 'no')
"""


def _hold_counterfactual_lines(rows: list[dict]) -> list[str]:
    """Per-category: as-is P&L ранних выходов vs hold-to-resolution counterfactual.

    Hold-PnL — та же формула, что у прод-settle по winner (_compute_settlement_pnl,
    honest per-category fee, 1 entry-нога), НЕ flat fee (урок D44).
    """
    from app.jobs.detector_signals_resolve import _signal_outcome
    from app.jobs.v2_execution_resolve import _compute_settlement_pnl
    from app.services.jsonb_utils import parse_jsonb

    per_cat: dict[str, dict] = {}
    for row in rows:
        fill_price = float(row["fill_price"] or 0.0)
        if fill_price <= 0:
            continue
        outcome = _signal_outcome(row["side"], row["winner"])
        if outcome is None:
            continue
        hold = _compute_settlement_pnl(
            stake=float(row["stake"]),
            fill_price=fill_price,
            outcome=outcome,
            category=row["category"],
            fee_schedule=parse_jsonb(row["fee_schedule"]),
        )["net_pnl"]
        agg = per_cat.setdefault(row["category"], {"asis": 0.0, "hold": 0.0, "orders": 0, "events": set()})
        agg["asis"] += float(row["net_pnl"])
        agg["hold"] += hold
        agg["orders"] += 1
        agg["events"].add(row["event_id"])

    lines = []
    for category, agg in sorted(per_cat.items(), key=lambda kv: kv[1]["hold"] - kv[1]["asis"], reverse=True):
        delta = agg["hold"] - agg["asis"]
        lines.append(
            f"• {category}: as-is {agg['asis']:+.2f}$ → hold {agg['hold']:+.2f}$ "
            f"(Δ{delta:+.2f}$, {agg['orders']} орд./{len(agg['events'])} соб.)"
        )
    return lines


def _format_gate_verdict_line(row: dict) -> str:
    net_pnl = float(row["net_pnl"])
    cell = f"{row['detector']} × {row['category']}"
    counts = f"{row['flagged']} сигн., {row['settled']} settled"
    if net_pnl < 0:
        return f"• {cell}: сэкономил бы {-net_pnl:+.2f}$ ({counts})"
    return f"• {cell}: упустил бы {net_pnl:+.2f}$ ({counts})"


def _trend_flips(rows: list[dict]) -> list[str]:
    """Ячейки со сменой знака ROI (30д vs пред. 30д) при n>=10 событий в обоих окнах."""
    lines = []
    for row in rows:
        if row["ev_recent"] < _TREND_MIN_EVENTS or row["ev_prior"] < _TREND_MIN_EVENTS:
            continue
        roi_recent, roi_prior = row["roi_recent"], row["roi_prior"]
        if roi_recent is None or roi_prior is None:
            continue
        roi_recent, roi_prior = float(roi_recent), float(roi_prior)
        if (roi_recent < 0) == (roi_prior < 0):
            continue
        arrow = "📉" if roi_recent < roi_prior else "📈"
        lines.append(
            f"{arrow} {row['detector']} × {row['category']}: "
            f"ROI {roi_prior:+.2f} → {roi_recent:+.2f} "
            f"({row['ev_prior']}/{row['ev_recent']} соб.)"
        )
    return lines


def _format_category_line(row: dict) -> str:
    net_pnl = float(row["net_pnl"])
    best = float(row["best_event_pnl"])
    worst = float(row["worst_event_pnl"])
    events = row["events"]

    line = f"• {row['category']}: {net_pnl:+.2f}$ ({events} соб., ROI {float(row['roi']):+.2f})"

    # Концентрация: одно событие делает больше половины |P&L| категории —
    # результат нельзя читать как свойство категории.
    dominant = best if abs(best) >= abs(worst) else worst
    if events >= 5 and net_pnl != 0 and abs(dominant) >= 0.5 * abs(net_pnl):
        line += f" ⚠️ топ-событие {dominant:+.2f}$"
    return line


async def run_virtual_weekly_digest() -> dict[str, Any]:
    if not await debounce_acquire("virtual_weekly_digest_running", ttl=900):
        return {"status": "skipped", "reason": "already_running_or_recent"}

    rows = await fetch(_VIRTUAL_WEEKLY_SQL)

    by_outcome = {row["outcome"]: {"n": row["n"], "net_pnl_sum": float(row["net_pnl_sum"])} for row in rows}
    total_trades = sum(v["n"] for v in by_outcome.values())

    if total_trades == 0:
        return {"status": "ok", "trades": 0}

    win = by_outcome.get("WIN", {"n": 0, "net_pnl_sum": 0.0})
    loss = by_outcome.get("LOSS", {"n": 0, "net_pnl_sum": 0.0})
    early_exit = by_outcome.get("EARLY_EXIT", {"n": 0, "net_pnl_sum": 0.0})
    expired = by_outcome.get("EXPIRED", {"n": 0, "net_pnl_sum": 0.0})
    net_sum = win["net_pnl_sum"] + loss["net_pnl_sum"] + early_exit["net_pnl_sum"] + expired["net_pnl_sum"]

    text = (
        f"📊 Virtual за неделю:\n"
        f"{win['n']} WIN ({win['net_pnl_sum']:+.2f}$) / "
        f"{loss['n']} LOSS ({loss['net_pnl_sum']:+.2f}$) / "
        f"{early_exit['n']} EXIT ({early_exit['net_pnl_sum']:+.2f}$) / "
        f"{expired['n']} EXPIRED\n"
        f"Net: {net_sum:+.2f}$"
    )

    category_rows = await fetch(_CATEGORY_WEEKLY_SQL)
    if category_rows:
        lines = "\n".join(_format_category_line(row) for row in category_rows)
        text += f"\n\nПо категориям (кластер = событие):\n{lines}"

    hold_lines = _hold_counterfactual_lines(await fetch(_HOLD_COUNTERFACTUAL_SQL))
    if hold_lines:
        text += "\n\nExit-политика: as-is vs hold (ранние выходы 60д, резолвнутые рынки):\n" + "\n".join(hold_lines)

    trend_lines = _trend_flips(await fetch(_TREND_SQL))
    if trend_lines:
        text += "\n\nСмена знака ROI (30д vs пред. 30д):\n" + "\n".join(trend_lines)

    verdict_rows = await fetch(_GATE_VERDICT_SQL)
    if verdict_rows:
        lines = "\n".join(_format_gate_verdict_line(row) for row in verdict_rows)
        text += f"\n\n🚧 Shadow-гейт категорий (контрфактика, всё время):\n{lines}"

    try:
        from app.tasks.notifications import send_admin_message

        await send_admin_message(text)
    except Exception as e:
        logger.warning("virtual_weekly_digest_notify_failed", error=str(e)[:200])

    return {"status": "ok", "trades": total_trades}
