"""Demote alerts monitor — мониторит virtual И live детекторы на drawdown.

PR-C из shadow stage переделки (research/shadow-gates-methodology-2026-05).
Phase 2 (audit): расширено на live-детекторы — раньше live был вне
demote-контура вообще (см. research/design-audit-2026-07-02.md).

Триггер: детектор (virtual ИЛИ live) за last 7d провалил signal-level
promote-экономику (G2 net_pnl / G5 Sortino, см. evaluate_demotion_candidates).
Action:
  - virtual: **alert-only** — TG admin. Demote (`stage='virtual' → 'shadow'`)
    делает человек через `/promote` команду или прямую SQL.
  - live: если `settings.live_auto_pause_enabled` — auto `mark_paused()`
    (stage='paused', обратимо через TG/SQL) + admin alert; иначе тоже
    alert-only (default — human decides, реальные деньги).

Дизайн (v3 conservative, см. detector_metrics.py compute_sortino):
  - Auto-action risk false positives в первой версии
  - Demote v→s "обратимо" (no monetary loss), но live→v требует human
  - Симметрия с manual promote через TG (см. CLAUDE.md "никогда не
    auto-promote в live" — auto-demote/pause safe с человеческим default,
    но включаемо осознанно через live_auto_pause_enabled)

Run: hourly (cron 'demote-alerts-monitor'). Идемпотентен — повторный alert
тот же detector в течение `_ALERT_DEDUPE_WINDOW_HOURS` (6 часов) **скипается**
через `demote_alert_log` table для anti-spam. Без идемпотентности TG будет
звонить каждый час пока не разрулят.

Окно названо константой, а не числом: раньше здесь стояло «1h», хотя константа
уже давно 6 — докстринг разошёлся с кодом и врал читающему.
"""

from __future__ import annotations

import logging
from typing import Any

import structlog

from app.analytics.notification import Notification, Severity
from app.analytics.subject_labels import humanize_subject
from app.core.config import get_settings
from app.core.detector_state import mark_paused
from app.services.database import execute, fetch
from app.services.detector_metrics import (
    evaluate_demotion_candidates,
    fetch_live_metrics_7d,
    fetch_shadow_signals_7d,
    fetch_virtual_metrics_7d,
)
from app.services.notification_bus import publish

logger = structlog.get_logger()

# Anti-spam: не повторять alert на тот же detector в течение N часов.
_ALERT_DEDUPE_WINDOW_HOURS = 6


_DEDUPE_QUERY_SQL = """
SELECT detector, MAX(alerted_at) AS last_alert
  FROM demote_alert_log
 WHERE alerted_at > now() - make_interval(hours => $1)
 GROUP BY detector
"""


_DEDUPE_INSERT_SQL = """
INSERT INTO demote_alert_log (detector, pnl_usd, n_settled, reason, alerted_at)
VALUES ($1, $2, $3, $4, now())
"""


def _candidate_line(cand: dict) -> str:
    sortino = cand.get("sortino")
    sortino_part = f", устойчивость к просадкам {sortino}" if sortino is not None else ""
    # Имя вычисляется отдельной строкой, а не вложенной f-строкой: вложение
    # одинаковых кавычек — синтаксис 3.12+, а прод и CI собирают на 3.11.
    name = humanize_subject("detector:" + cand["detector"])
    return (
        f"{name}: за неделю доходность сигналов "
        f"{cand['sum_roi_net']:.1f}%{sortino_part} на {cand['n_terminal']} завершённых сделках."
    )


def to_notifications(
    virtual_fresh: list[dict],
    live_fresh: list[dict],
    *,
    auto_paused: list[str],
    auto_pause_enabled: bool,
) -> list[Notification]:
    """Кандидаты на понижение → Notification (спека §3).

    Одно уведомление НА ДЕТЕКТОР с subject='detector:{id}': subject это ключ
    склейки (§3), и per-detector subject даёт шине объединить просадку с
    edge_decay по тому же детектору в одно сообщение вместо двух про одну беду.

    Live и virtual различаются не текстом, а последствием: у live деньги
    настоящие, поэтому там либо уже сработала авто-пауза, либо нужно решать
    руками. what_if_ignored заполняется ТОЛЬКО для live — у virtual убыток
    бумажный, и подать его как потерю денег значит соврать.
    """
    out: list[Notification] = []

    for cand in virtual_fresh:
        out.append(
            Notification(
                severity=Severity.ACTION_REQUIRED,
                subject=f"detector:{cand['detector']}",
                kind="demote_candidate",
                headline="Детектор просел — стоит понизить в тень",
                what_happened=[
                    _candidate_line(cand),
                    "Детектор торгует на бумаге, реальных денег он не теряет.",
                ],
                what_if_ignored=None,
                what_to_do=(
                    "Решай, понижать ли его в тень. Автоматически ничего не изменено — "
                    "виртуальные детекторы понижаются только вручную."
                ),
                actions=[],
                evidence=[f"{cand['detector']}: sum_roi_net={cand['sum_roi_net']:.2f}%, причина: {cand['reason']}"],
            )
        )

    for cand in live_fresh:
        paused = cand["detector"] in auto_paused
        if paused:
            what_to_do = (
                "Детектор уже остановлен автоматически. Разберись в причине просадки перед тем, как включать обратно."
            )
        elif auto_pause_enabled:
            what_to_do = (
                "Автопауза включена, но остановить этот детектор не удалось — "
                "останови его вручную и проверь лог ошибки."
            )
        else:
            what_to_do = (
                "Автопауза выключена, детектор продолжает торговать. Останови его вручную, если просадка не объяснима."
            )

        out.append(
            Notification(
                severity=Severity.ACTION_REQUIRED,
                subject=f"detector:{cand['detector']}",
                kind="demote_candidate_live",
                headline=("Детектор остановлен из-за просадки" if paused else "Детектор в живой торговле просел"),
                what_happened=[
                    _candidate_line(cand),
                    (
                        "Он торгует настоящими деньгами и уже остановлен."
                        if paused
                        else "Он торгует настоящими деньгами и продолжает работать."
                    ),
                ],
                what_if_ignored=(
                    None
                    if paused
                    else (
                        f"Доходность его сигналов за неделю {cand['sum_roi_net']:.1f}%. "
                        "Пока он работает, эта просадка продолжает набираться на реальных деньгах."
                    )
                ),
                what_to_do=what_to_do,
                actions=[],
                evidence=[f"{cand['detector']}: sum_roi_net={cand['sum_roi_net']:.2f}%, причина: {cand['reason']}"],
            )
        )

    return out


async def _load_recent_alerts() -> set[str]:
    """Detectors которые мы alert'или в последние _ALERT_DEDUPE_WINDOW_HOURS."""
    try:
        rows = await fetch(_DEDUPE_QUERY_SQL, _ALERT_DEDUPE_WINDOW_HOURS)
    except Exception as exc:
        # demote_alert_log table может не существовать на dev/test окружениях.
        # Fail-open: лучше повторить alert один раз, чем не отправить вообще.
        logger.warning("demote_alert_log_unavailable", error=str(exc))
        return set()
    return {r["detector"] for r in rows}


async def _record_alert(detector: str, pnl_usd: float, n_settled: int, reason: str) -> None:
    try:
        await execute(_DEDUPE_INSERT_SQL, detector, pnl_usd, n_settled, reason)
    except Exception as exc:
        # Не валим job из-за лога — alert уже отправлен.
        logger.warning("demote_alert_log_write_failed", detector=detector, error=str(exc))


async def run_demote_alerts_monitor() -> dict[str, Any]:
    """Hourly check: alert на virtual И live детекторы с большим drawdown.

    Phase 2 (audit): live-детекторы теперь тоже под мониторингом (раньше
    только virtual). Live-кандидаты помечаются is_live=True — для них
    settings.live_auto_pause_enabled решает mark_paused() (auto) vs alert-only.

    Returns: dict со счётчиками для observability.
    """
    virtual_metrics = await fetch_virtual_metrics_7d()
    live_metrics = await fetch_live_metrics_7d()
    if not virtual_metrics and not live_metrics:
        logger.info("demote_alerts_no_virtual")
        return {"n_virtual": 0, "n_candidates": 0, "n_alerted": 0, "n_deduped": 0}

    shadow_signals = await fetch_shadow_signals_7d()
    candidates = evaluate_demotion_candidates(virtual_metrics, shadow_signals)
    for c in candidates:
        c["is_live"] = False
    live_candidates = evaluate_demotion_candidates(live_metrics, shadow_signals)
    for c in live_candidates:
        c["is_live"] = True
    candidates.extend(live_candidates)

    if not candidates:
        logger.info("demote_alerts_no_candidates", n_virtual=len(virtual_metrics))
        return {"n_virtual": len(virtual_metrics), "n_candidates": 0, "n_alerted": 0, "n_deduped": 0}

    recent_alerts = await _load_recent_alerts()

    fresh_candidates = [c for c in candidates if c["detector"] not in recent_alerts]
    deduped_count = len(candidates) - len(fresh_candidates)

    if not fresh_candidates:
        logger.info(
            "demote_alerts_all_deduped",
            n_candidates=len(candidates),
            window_hours=_ALERT_DEDUPE_WINDOW_HOURS,
        )
        return {
            "n_virtual": len(virtual_metrics),
            "n_candidates": len(candidates),
            "n_alerted": 0,
            "n_deduped": deduped_count,
        }

    # Telegram alert
    settings = get_settings()

    virtual_fresh = [c for c in fresh_candidates if not c["is_live"]]
    live_fresh = [c for c in fresh_candidates if c["is_live"]]

    # Auto-pause выполняется ДО сборки сообщения и отдельно от неё. Раньше
    # mark_paused() дёргался прямо внутри цикла построения текста — побочный
    # эффект с деньгами, спрятанный в форматировании: сбой рендера мог оставить
    # детектор непаузенным, а перестановка строк местами меняла бы поведение.
    live_auto_paused: list[str] = []
    if live_fresh and settings.live_auto_pause_enabled:
        for cand in live_fresh:
            try:
                await mark_paused(cand["detector"], f"auto_demote:{cand['reason'][:100]}")
                live_auto_paused.append(cand["detector"])
            except Exception:
                logger.exception("live_auto_pause_failed", detector=cand["detector"])

    # Сбой отправки не должен ронять джобу: ниже по коду дедуп-ключи пишутся
    # только при n_alerted > 0, то есть провал сам по себе оставляет кандидатов
    # для retry на следующем тике. Падение с исключением сломало бы и логи,
    # и возвращаемый результат, хотя auto-pause выше уже отработал.
    try:
        published = await publish(
            to_notifications(
                virtual_fresh,
                live_fresh,
                auto_paused=live_auto_paused,
                auto_pause_enabled=settings.live_auto_pause_enabled,
            ),
            actor="demote_alerts_monitor",
        )
        n_alerted = published.sent
    except Exception:
        logger.exception("demote_alerts_publish_failed")
        n_alerted = 0

    # Записываем dedupe ключи только если TG отправлен (иначе следующий tick
    # сможет retry — failure не должен молча проглотить alert).
    if n_alerted > 0:
        for cand in fresh_candidates:
            # demote_alert_log колонки pnl_usd/n_settled переиспользуются под
            # v4-семантику (sum_roi_net %, n_terminal) — schema без миграции,
            # лог нужен только для anti-spam dedup по detector+alerted_at.
            await _record_alert(
                cand["detector"],
                float(cand["sum_roi_net"]),
                int(cand["n_terminal"]),
                str(cand["reason"]),
            )

    logger.info(
        "demote_alerts_completed",
        n_candidates=len(candidates),
        n_alerted=len(fresh_candidates) if n_alerted > 0 else 0,
        n_deduped=deduped_count,
        n_admin_recipients=n_alerted,
        n_live_auto_paused=len(live_auto_paused),
    )

    return {
        "n_virtual": len(virtual_metrics),
        "n_candidates": len(candidates),
        "n_alerted": len(fresh_candidates) if n_alerted > 0 else 0,
        "n_deduped": deduped_count,
        "n_live_auto_paused": len(live_auto_paused),
    }


# Default log level если запускается через cron_runner.
logging.basicConfig(level=logging.INFO)
