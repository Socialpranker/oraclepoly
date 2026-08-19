"""Detector SLO monitor — daily check signal emission rate per detector.

Цель: catch broken pipeline в течение 24h, не через неделю.

Алёртим только когда детектор:
  1. Реально активен (cron-строка не закомментирована в crontab.conf), И
  2. Не в self-pause (paused_reason IS NULL в detector_state), И
  3. Имеет baseline emission rate в EXPECTED_RATE_PER_DAY, И
  4. Actual emission за 24h < 30% от baseline.

Source of truth для "детектор должен слать сигналы" — committed crontab.conf,
а не только detector_state в БД. Когда детектор официально "cut" — cron-строку
комментируют (см. PR #449 Phase 0 Stage 6), но detector_state row остаётся.
Если читать только БД, monitor будет ругаться на cut detectors.

Run: daily 06:00 UTC (after detector-signals-resolve at :40).
"""

from __future__ import annotations

import logging
from typing import Any

import structlog

from app.analytics.notification import Notification, Severity
from app.analytics.subject_labels import humanize_subject
from app.services.database import fetch
from app.services.detector_metrics import (
    fetch_db_active_detectors,
    parse_active_cron_detectors,
)
from app.services.notification_bus import publish

logger = structlog.get_logger()

# Expected emission rate per detector (signals/day, from canonical
# backtest `trades_per_day`). Conservatively low — alert at 30% threshold.
# Add detector here при promote из shadow в virtual.
# Убрать detector из списка если cron убрали или детектор self-disables по логике.
EXPECTED_RATE_PER_DAY: dict[str, float] = {
    "D3_cross_mkt_arb": 0.37,
    # D13: self-disables при negative virtual PnL (см. d13_scan.py:175,
    # `D13_EMIT_DISABLED = True`). Источник истины для "детектор отключён" —
    # detector_state.paused_reason='auto_disabled_...' на проде (выставлено
    # 2026-05-25). Monitor автоматически скипнет через paused_reason IS NULL
    # фильтр. Baseline тут оставляем чтобы при reactivate (paused_reason=NULL +
    # D13_EMIT_DISABLED=False) сразу включился SLO check.
    "D13_retail_confidence_fade": 4.2,  # combined 4h+24h+72h emission
    # D14: zero markets часто — узкая ниша (crypto winners distribute NO,
    # mid zone [0.50, 0.70) на 4h). За 30d на проде ноль signals/day
    # (2026-05-25 проверка). Опускаем под MIN_EXPECTED_24H_FOR_RATIO_CHECK=2.0
    # → попадёт в low_rate_skip, ratio check не делается. Поднять обратно
    # когда реальная эмиссия > 2/day стабильно.
    "D14_crypto_winners_distribute_no": 0.5,
}

# Alert threshold: actual / expected < this → alert.
# 0.3 значит "rate упала ниже 30% baseline за 24h" — что-то поломалось.
ALERT_RATIO_THRESHOLD = 0.3

# Минимум сигналов — для детекторов с low expected rate (D3 0.37/d, D14 0.5/d)
# 24h sample слишком мал для надёжного ratio. Подключаем ratio только если
# expected_24h >= MIN_EXPECTED_24H_FOR_RATIO_CHECK.
MIN_EXPECTED_24H_FOR_RATIO_CHECK = 2.0

_QUERY_SQL = """
SELECT detector, COUNT(*) AS n_24h
  FROM detector_signals
 WHERE source = 'live_v2'
   AND generated_at > now() - INTERVAL '24 hours'
 GROUP BY detector
"""


def to_notifications(alerts: list[dict[str, Any]]) -> list[Notification]:
    """Падение emission rate → Notification (спека §3).

    Одно уведомление НА ДЕТЕКТОР, а не одно общее: subject — ключ склейки (§3),
    и per-detector subject позволяет шине объединить это с edge_decay по тому же
    детектору в одно сообщение. Общий subject вида "infra:detector_emission"
    такую склейку убил бы — ровно дубль D38 из §1 п.4, только наоборот.

    what_if_ignored=None: детектор мог замолчать и в shadow, и в live — сумма
    зависит от стадии, которой здесь нет на руках. Выдумывать её §3 запрещает.
    """
    out: list[Notification] = []
    for a in alerts:
        det = a["detector"]
        out.append(
            Notification(
                severity=Severity.ACTION_REQUIRED,
                subject=f"detector:{det}",
                kind="emission_drop",
                headline="Детектор почти перестал находить сигналы",
                what_happened=[
                    f"За сутки {a['actual']} сигналов вместо обычных ~{a['expected_24h']:.0f} "
                    f"— это {a['ratio']:.0%} от нормы.",
                    "Молчащий детектор выглядит в отчётах как «нет подходящих рынков», "
                    "хотя причина обычно техническая.",
                ],
                what_if_ignored=None,
                what_to_do=(
                    f"Посмотри лог сканера {det} за последние прогоны: чаще всего это "
                    "самоотключение детектора, изменившийся фильтр рынков или лимит на стороне Polymarket."
                ),
                actions=[],
                evidence=[
                    f"{humanize_subject(f'detector:{det}')}: {a['actual']}/24ч против ожидаемых "
                    f"{a['expected_24h']:.1f} (ratio={a['ratio']:.2f})",
                    f"tail -100 /var/log/oracle/{det}-scan.log",
                ],
            )
        )
    return out


async def run_detector_slo_check() -> dict[str, Any]:
    """Check signal emission rate vs baseline per active detector."""
    rows_signals = await fetch(_QUERY_SQL)
    actual: dict[str, int] = {r["detector"]: int(r["n_24h"]) for r in rows_signals}

    db_active_detectors = await fetch_db_active_detectors()
    cron_active_detectors = parse_active_cron_detectors()

    # Detector реально работает только если активен В БД И есть активный cron.
    # Cut детекторы (cron закомментирован) — molchim даже если в БД shadow.
    effective_active = db_active_detectors & cron_active_detectors

    skipped_no_cron = db_active_detectors - cron_active_detectors
    if skipped_no_cron:
        logger.info("detector_slo_skipped_no_cron", detectors=sorted(skipped_no_cron))

    alerts: list[str] = []
    breakdown: list[dict[str, Any]] = []

    for det in sorted(effective_active):
        expected_per_day = EXPECTED_RATE_PER_DAY.get(det)
        if expected_per_day is None:
            # Detector активный но baseline не задан — пропускаем (но логируем)
            logger.warning("detector_slo_no_baseline", detector=det)
            breakdown.append({"detector": det, "actual": actual.get(det, 0), "status": "no_baseline"})
            continue

        actual_n = actual.get(det, 0)
        expected_24h = expected_per_day  # already per-day

        if expected_24h < MIN_EXPECTED_24H_FOR_RATIO_CHECK:
            # Low-rate detector: alert ТОЛЬКО при 0 signals за 48h (separate path)
            # Здесь skip — проверяется отдельно если нужно
            breakdown.append(
                {
                    "detector": det,
                    "actual": actual_n,
                    "expected_24h": round(expected_24h, 2),
                    "status": "low_rate_skip",
                }
            )
            continue

        ratio = actual_n / expected_24h if expected_24h > 0 else 0
        status = "ok" if ratio >= ALERT_RATIO_THRESHOLD else "alert"
        breakdown.append(
            {
                "detector": det,
                "actual": actual_n,
                "expected_24h": round(expected_24h, 2),
                "ratio": round(ratio, 2),
                "status": status,
            }
        )

        if status == "alert":
            # Структурно, а не готовой HTML-строкой: текст собирает рендерер
            # конверта, джоба отдаёт факты (спека §2 п.1).
            alerts.append({"detector": det, "actual": actual_n, "expected_24h": expected_24h, "ratio": ratio})

    # Telegram alert — если есть problems
    if alerts:
        await publish(to_notifications(alerts), actor="detector_slo_monitor")

    n_alerts = len(alerts)
    n_ok = sum(1 for b in breakdown if b.get("status") == "ok")
    logger.info(
        "detector_slo_completed",
        n_db_active=len(db_active_detectors),
        n_cron_active=len(cron_active_detectors),
        n_effective_active=len(effective_active),
        n_skipped_no_cron=len(skipped_no_cron),
        n_alerts=n_alerts,
        n_ok=n_ok,
        n_no_baseline=sum(1 for b in breakdown if b.get("status") == "no_baseline"),
        n_low_rate=sum(1 for b in breakdown if b.get("status") == "low_rate_skip"),
    )

    return {
        "n_db_active": len(db_active_detectors),
        "n_cron_active": len(cron_active_detectors),
        "n_effective_active": len(effective_active),
        "n_skipped_no_cron": len(skipped_no_cron),
        "alerts": n_alerts,
        "ok": n_ok,
        "breakdown": breakdown,
    }


# Default level если запускается через cron_runner
logging.basicConfig(level=logging.INFO)
