"""Daily Virtual Summary — отправляем в TG раз в день.

Содержит:
  - 24h activity (placed/filled/settled, win-rate)
  - Virtual P&L: 24h vs предыдущие 24h, дельта, 7d cumulative
  - Top/bottom 3 detectors по P&L
  - System health: active detectors (cron + db)

Источник правды — execution_orders (virtual mode + simulation_model_version=
v2_conservative + outcome != VOIDED_BUG). Та же логика что в
fetch_virtual_metrics_7d, но окно 24h + сравнение с предыдущими 24h.

Cron: daily 06:00 UTC (09:00 МСК), после resolve job в 06:40 UTC предыдущего
дня. Так PnL за вчера уже settled к моменту отправки.
"""

from __future__ import annotations

import logging
from typing import Any

import structlog

from app.core.config import get_settings
from app.services.database import fetch

logger = structlog.get_logger(__name__)

# Сколько top/bottom detectors показывать. 3 — компромисс между context и spam.
N_TOP_DETECTORS = 3

# Daily activity + P&L за два окна (last 24h vs previous 24h) одним запросом.
# Фильтры повторяют detector_metrics._VIRTUAL_METRICS_7D_SQL для consistency.
_DAILY_ACTIVITY_SQL = """
SELECT
    detector,
    COUNT(*) FILTER (
        WHERE status IN ('virtual_placed', 'virtual_filled', 'virtual_settled', 'rejected')
          AND created_at > now() - INTERVAL '24 hours'
    ) AS placed_24h,
    COUNT(*) FILTER (
        WHERE status IN ('virtual_filled', 'virtual_settled')
          AND COALESCE(outcome, '') != 'VOIDED_BUG'
          AND created_at > now() - INTERVAL '24 hours'
    ) AS filled_24h,
    COUNT(*) FILTER (
        WHERE status = 'virtual_settled'
          AND COALESCE(outcome, '') != 'VOIDED_BUG'
          AND created_at > now() - INTERVAL '24 hours'
    ) AS settled_24h,
    COUNT(*) FILTER (
        WHERE status = 'virtual_settled' AND outcome = 'WIN'
          AND created_at > now() - INTERVAL '24 hours'
    ) AS wins_24h,
    COUNT(*) FILTER (
        WHERE status = 'virtual_settled' AND outcome = 'LOSS'
          AND created_at > now() - INTERVAL '24 hours'
    ) AS losses_24h,
    COUNT(*) FILTER (
        WHERE status = 'virtual_settled' AND outcome = 'EARLY_EXIT'
          AND created_at > now() - INTERVAL '24 hours'
    ) AS early_exits_24h,
    -- EARLY_EXIT с положительным P&L — proxy для "выиграл бы при resolution"
    COUNT(*) FILTER (
        WHERE status = 'virtual_settled' AND outcome = 'EARLY_EXIT'
          AND COALESCE(net_pnl, pnl) > 0
          AND created_at > now() - INTERVAL '24 hours'
    ) AS early_exits_win_24h,
    COALESCE(SUM(COALESCE(net_pnl, pnl)) FILTER (
        WHERE status = 'virtual_settled'
          AND COALESCE(outcome, '') != 'VOIDED_BUG'
          AND created_at > now() - INTERVAL '24 hours'
    ), 0) AS pnl_24h,
    COALESCE(SUM(COALESCE(net_pnl, pnl)) FILTER (
        WHERE status = 'virtual_settled'
          AND COALESCE(outcome, '') != 'VOIDED_BUG'
          AND created_at <= now() - INTERVAL '24 hours'
          AND created_at > now() - INTERVAL '48 hours'
    ), 0) AS pnl_prev_24h,
    COALESCE(SUM(COALESCE(net_pnl, pnl)) FILTER (
        WHERE status = 'virtual_settled'
          AND COALESCE(outcome, '') != 'VOIDED_BUG'
          AND created_at > now() - INTERVAL '7 days'
    ), 0) AS pnl_7d
FROM execution_orders
WHERE execution_mode = 'virtual'
  AND simulation_model_version = 'v2_conservative'
  AND created_at > now() - INTERVAL '7 days'
  -- Исключаем re-entry (meta.reentry_excluded=true) для consistency с гейтом и
  -- дашбордом. См. standalone_virtual_pnl_inflation_2026-06-04.
  AND COALESCE((meta->>'reentry_excluded')::bool, false) = false
GROUP BY detector
"""


def _format_delta(today: float, yesterday: float) -> str:
    """Δ-строка вида '+$33.30 (+35%)' или '-$45.00 (—)' если yesterday=0."""
    delta = today - yesterday
    sign = "+" if delta >= 0 else "−"
    abs_delta = abs(delta)
    if abs(yesterday) < 0.01:
        pct = "—"
    else:
        pct_val = (delta / abs(yesterday)) * 100
        pct = f"{'+' if pct_val >= 0 else ''}{pct_val:.0f}%"
    return f"{sign}${abs_delta:.2f} ({pct})"


def _build_message(rows: list[dict[str, Any]], n_active_db: int, n_active_cron: int) -> str:
    """Pure formatter — отдельно от I/O для тестируемости."""
    # Aggregate totals across all detectors
    total_placed_24h = sum(int(r["placed_24h"]) for r in rows)
    total_filled_24h = sum(int(r["filled_24h"]) for r in rows)
    total_settled_24h = sum(int(r["settled_24h"]) for r in rows)
    total_wins = sum(int(r["wins_24h"]) for r in rows)
    total_losses = sum(int(r["losses_24h"]) for r in rows)
    total_early_exits = sum(int(r["early_exits_24h"]) for r in rows)
    total_early_exits_win = sum(int(r["early_exits_win_24h"]) for r in rows)
    total_pnl_24h = sum(float(r["pnl_24h"] or 0) for r in rows)
    total_pnl_prev_24h = sum(float(r["pnl_prev_24h"] or 0) for r in rows)
    total_pnl_7d = sum(float(r["pnl_7d"] or 0) for r in rows)

    # Per-detector 24h PnL — для top (positive only) и bottom (negative only)
    per_detector_24h = [
        (r["detector"], float(r["pnl_24h"] or 0), int(r["settled_24h"])) for r in rows if int(r["settled_24h"]) > 0
    ]
    winners = sorted([x for x in per_detector_24h if x[1] > 0], key=lambda x: x[1], reverse=True)
    losers = sorted([x for x in per_detector_24h if x[1] < 0], key=lambda x: x[1])
    top = winners[:N_TOP_DETECTORS]
    bottom = losers[:N_TOP_DETECTORS]

    # WIN/LOSS resolved → final outcome rate. EARLY_EXIT — отдельный блок,
    # потому что mark-to-market exit ≠ финальный resolve и в текущем pipeline
    # их подавляющее большинство (>90% virtual trades exit'ятся через TP/SL).
    n_resolved = total_wins + total_losses
    resolved_win_rate = (total_wins / n_resolved * 100) if n_resolved > 0 else 0.0
    ee_win_rate = (total_early_exits_win / total_early_exits * 100) if total_early_exits > 0 else 0.0

    lines = [
        "📊 <b>OraclePoly Daily Summary</b>",
        "",
        "<b>24h Activity</b>",
        f"  Placed: <b>{total_placed_24h}</b>  ·  Filled: {total_filled_24h}  ·  Settled: {total_settled_24h}",
    ]
    if n_resolved > 0:
        lines.append(f"  Resolved: WIN <b>{total_wins}</b> ({resolved_win_rate:.0f}%)  ·  LOSS {total_losses}")
    if total_early_exits > 0:
        lines.append(f"  Early exits: {total_early_exits} ({total_early_exits_win} в плюс = {ee_win_rate:.0f}%)")
    lines.extend(
        [
            "",
            "<b>Virtual P&amp;L</b>",
            f"  Today:     {'+' if total_pnl_24h >= 0 else '−'}${abs(total_pnl_24h):.2f}",
            f"  Yesterday: {'+' if total_pnl_prev_24h >= 0 else '−'}${abs(total_pnl_prev_24h):.2f}",
            f"  Δ:         {_format_delta(total_pnl_24h, total_pnl_prev_24h)}",
            f"  7d sum:    {'+' if total_pnl_7d >= 0 else '−'}${abs(total_pnl_7d):.2f}",
        ]
    )

    if top:
        lines.extend(["", "<b>🏆 Top (24h)</b>"])
        for det, pnl, n in top:
            sign = "+" if pnl >= 0 else "−"
            lines.append(f"  {det}: {sign}${abs(pnl):.2f} ({n} trades)")

    if bottom:
        lines.extend(["", "<b>📉 Bottom (24h)</b>"])
        for det, pnl, n in bottom:
            lines.append(f"  {det}: −${abs(pnl):.2f} ({n} trades)")

    lines.extend(
        [
            "",
            "<b>⚙️ System</b>",
            f"  Active detectors: {n_active_cron} (cron) · {n_active_db} (db)",
        ]
    )

    return "\n".join(lines)


async def run_daily_summary() -> dict[str, Any]:
    """Собрать summary за 24h и отправить admin'ам."""
    from app.services.detector_metrics import fetch_db_active_detectors, parse_active_cron_detectors

    rows = await fetch(_DAILY_ACTIVITY_SQL)
    n_active_db = len(await fetch_db_active_detectors())
    n_active_cron = len(parse_active_cron_detectors())

    text = _build_message(list(rows), int(n_active_db), n_active_cron)

    settings = get_settings()
    from app.api.telegram import send_telegram_message

    n_sent = 0
    for admin_id in settings.admin_telegram_ids:
        try:
            await send_telegram_message(admin_id, text)
            n_sent += 1
        except Exception:
            logger.exception("daily_summary_telegram_failed", admin_id=admin_id)

    logger.info("daily_summary_sent", n_detectors_with_data=len(rows), n_sent=n_sent)
    return {"n_detectors_with_data": len(rows), "n_sent": n_sent}


logging.basicConfig(level=logging.INFO)
