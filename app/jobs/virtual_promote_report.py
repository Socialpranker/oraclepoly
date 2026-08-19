"""Virtual Promote Report — ежедневный TG отчёт по gate-статусу виртуальных детекторов.

Показывает для каждого virtual детектора:
  - Сколько дней в virtual
  - n_terminal, avg_roi_net, sortino
  - Сколько gates прошло из 6
  - Конкретный блокер (что нужно исправить для promote)

Cron: 08:00 UTC ежедневно (после daily-summary в 06:00).
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

import structlog

from app.analytics.notification import Notification, Severity
from app.analytics.subject_labels import humanize_subject
from app.services.detector_metrics import (
    _SORTINO_THRESHOLD,
    compute_sortino,
    fetch_virtual_metrics_7d,
    fetch_virtual_period_signals,
    fetch_virtual_state_rows,
)
from app.services.notification_bus import publish

logger = structlog.get_logger(__name__)

_MIN_N_FOR_SORTINO = 10


def _compute_blocker(
    n_terminal: int,
    avg_roi: float | None,
    sortino: float | None,
    days_virtual: int,
    adversarial_pass: bool | None,
    execution_enabled: bool,
) -> str:
    """Вернуть строку с первым блокером по приоритету (как в evaluate_promote_gates)."""
    if n_terminal < 40:
        return f"n&lt;40 ({n_terminal})"
    if adversarial_pass is None:
        return "adversarial=NULL (нужна ручная оценка)"
    if adversarial_pass is False:
        return "adversarial=FAIL"
    if avg_roi is None or avg_roi <= 0:
        return "avg_roi≤0"
    if sortino is None or sortino < _SORTINO_THRESHOLD:
        s = f"{sortino:.2f}" if sortino is not None else "—"
        return f"sortino&lt;1 ({s})"
    if days_virtual < 14:
        return f"days&lt;14 ({days_virtual}d)"
    if not execution_enabled:
        return "EXECUTION_ENABLED=false"
    return "READY"


def _format_resolved_slice(row: dict[str, Any]) -> str:
    """Resolution-only (live-реплицируемый) P&L/winrate — order-level срез
    execution_orders, где outcome IN ('WIN','LOSS'), т.е. позиция реально
    дожила до резолюции рынка, а не была закрыта early-exit'ом (TP/SL/MZE),
    которого live-контур не исполняет (settings.live_exit_enabled=False).

    research/virtual-stage-audit-2026-07-05: as-is order-level virtual P&L
    может врать (D42 −$2.7k as-is vs +$0.8k live-реплицируемый — знак меняется).
    Метка нужна для переходного периода — историческим данным ДО exit-паритет
    фикса (position_manager.py) он не помогает, только честная резолюционная
    проекция показывает, что реально повторил бы live.
    """
    pnl = row.get("resolved_pnl_usd")
    wins = int(row.get("resolved_wins") or 0)
    losses = int(row.get("resolved_losses") or 0)
    n_resolved = wins + losses

    if pnl is None or n_resolved == 0:
        return "live-реплицируемый: —"

    pnl_str = f"{pnl:+.1f}"
    wr_pct = round(100.0 * wins / n_resolved, 1)
    return f"live-реплицируемый: {pnl_str}$ WR={wr_pct}% (n={n_resolved})"


def _format_report_line(row: dict[str, Any]) -> str:
    """Одна строка отчёта для детектора."""
    det = row["detector"]
    days = row["days_virtual"]
    n = row["n_terminal"]
    avg_roi = row["avg_roi_net"]
    sortino = row["sortino"]
    gates_passed = row["gates_passed"]
    gates_total = row["gates_total"]
    blocker = row["blocker"]

    roi_str = f"{avg_roi:+.1f}%" if avg_roi is not None else "—"
    sortino_str = f"{sortino:.2f}" if sortino is not None else "—"
    n_str = f"n={n:,}" if n >= 1000 else f"n={n}"

    status = "✅" if blocker == "READY" else "❌" if n > 0 else "⏳"
    # gates_passed/gates_total через дефис чтобы избежать TG HTML parse ошибки:
    # Telegram HTML парсер воспринимает "5/6" как "<5/6>" (незакрытый тег).
    line = (
        f"{status} {det} | {days}d | {n_str} | "
        f"ROI={roi_str} | Sortino={sortino_str} | "
        f"gates={gates_passed}-{gates_total} | {blocker}"
    )
    return f"{line} | {_format_resolved_slice(row)}"


def _format_message(rows: list[dict[str, Any]], today: str) -> str:
    """Собрать полное TG-сообщение."""
    lines = [
        f"📋 <b>Virtual Promote Report — {today}</b>",
        "",
    ]
    if not rows:
        lines.append("нет виртуальных детекторов")
        return "\n".join(lines)

    for row in rows:
        lines.append(_format_report_line(row))

    ready_count = sum(1 for r in rows if r["blocker"] == "READY")
    if ready_count > 0:
        lines.extend(["", f"🚀 <b>{ready_count} детектор(ов) READY для promote!</b>"])
    else:
        lines.extend(["", "Promote-кандидатов пока нет."])

    return "\n".join(lines)


def to_notifications(rows: list[dict[str, Any]]) -> list[Notification]:
    """Отчёт по virtual-детекторам → Notification (спека §3, §4).

    Severity по факту, а не константой. Отчёт шлётся по расписанию независимо
    от результата, и §4 велит такие мониторы делать FYI — но детектор в статусе
    READY это состояние с конкретным действием владельца (промоут в live —
    только human decision, никогда автоматически). Увести READY в дайджест
    значит потерять решение (риск №4 §10).

    what_if_ignored заполняется ТОЛЬКО у READY и только из фактического
    `resolved_pnl_usd` — это деньги по реально дожившим до резолва позициям.
    Нет цифры — нет строки: §3 запрещает выдумывать.
    """
    if not rows:
        return []

    ready = [r for r in rows if r["blocker"] == "READY"]

    if not ready:
        blocked = ", ".join(f"{r['detector']} ({r['gates_passed']}/6)" for r in rows[:5])
        return [
            Notification(
                severity=Severity.FYI,
                subject="analytics:virtual_promote",
                kind="virtual_promote_report",
                headline="Кандидатов на продвижение пока нет",
                what_happened=[
                    f"Проверено детекторов на бумажной торговле: {len(rows)}. Ни один не прошёл все гейты.",
                    f"Ближайшие: {blocked}." if blocked else "",
                ],
                what_if_ignored=None,
                what_to_do=None,
                actions=[],
                evidence=[
                    f"{r['detector']}: гейтов {r['gates_passed']}/6, стоп — {r['blocker']}, сделок {r['n_terminal']}"
                    for r in rows
                ],
            )
        ]

    lines = []
    money = None
    for r in ready:
        name = humanize_subject("detector:" + r["detector"])
        pnl = r.get("resolved_pnl_usd")
        line = f"{name}: прошёл все проверки на {r['n_terminal']} завершённых сделках."
        if pnl is not None:
            line += f" На бумаге это ${float(pnl):.0f} по позициям, дожившим до резолва."
            if money is None:
                money = float(pnl)
        lines.append(line)
    lines.append("Продвижение в живую торговлю — только твоё решение, автоматически ничего не произойдёт.")

    cost = None
    if money is not None and money > 0:
        cost = (
            f"Пока детектор на бумаге, эти ${money:.0f} остаются виртуальными. "
            "Настоящими они станут только после продвижения в live — как и убыток, если он придёт."
        )

    return [
        Notification(
            severity=Severity.ACTION_REQUIRED,
            subject="analytics:virtual_promote",
            kind="virtual_promote_ready",
            headline=f"Детекторов готово к продвижению: {len(ready)}",
            what_happened=lines,
            what_if_ignored=cost,
            what_to_do=(
                "Посмотри их метрики и реши, продвигать ли в живую торговлю. Решение принимается только вручную."
            ),
            actions=[],
            evidence=[
                f"{r['detector']}: гейтов {r['gates_passed']}/6, сделок {r['n_terminal']}, "
                f"resolved_pnl={r.get('resolved_pnl_usd')}"
                for r in ready
            ],
        )
    ]


async def run_virtual_promote_report() -> dict[str, Any]:
    """Собрать gate-статус виртуальных детекторов и отправить через шину."""
    from app.core.config import get_settings

    # execution_enabled — один из шести гейтов промоута, читается ниже.
    settings = get_settings()
    today = datetime.now(UTC).strftime("%Y-%m-%d")

    state_rows = await fetch_virtual_state_rows()
    if not state_rows:
        logger.info("virtual_promote_report_no_virtual_detectors")
        return {"n_virtual": 0, "n_sent": 0}

    signal_map = await fetch_virtual_period_signals([(sr["detector"], sr["promoted_at"]) for sr in state_rows])
    # Resolution-only (live-реплицируемый) order-level срез — audit
    # virtual-stage-audit-2026-07-05 фикс #1: as-is order-level virtual P&L
    # (paper EARLY_EXIT включён) может врать в обе стороны, только позиции,
    # дожившие до реальной резолюции (outcome IN WIN/LOSS), честно проецируют
    # то, что исполнил бы live BUY-only hold-to-resolution контур.
    resolved_map = await fetch_virtual_metrics_7d()

    rows: list[dict[str, Any]] = []
    for sr in state_rows:
        det = sr["detector"]
        promoted_at = sr["promoted_at"]
        days_virtual = (datetime.now(UTC) - promoted_at).days if promoted_at else 0
        adversarial_pass = sr.get("adversarial_mirror_pass")

        sd = signal_map.get(det) or {}
        rd = resolved_map.get(det) or {}
        n_terminal = int(sd.get("terminal_7d") or 0)
        mean_roi = sd.get("mean_roi_net_7d")
        sum_neg_sq = float(sd.get("sum_neg_sq_roi_7d") or 0)
        avg_roi = float(mean_roi) if mean_roi is not None else None
        sum_roi = float(sd.get("sum_roi_net_7d") or 0)

        sortino = compute_sortino(mean_roi, sum_neg_sq, n_terminal) if n_terminal >= _MIN_N_FOR_SORTINO else None

        # Упрощённый подсчёт gates для отчёта (без Wilson WR и fill_rate).
        # Точный вердикт — evaluate_promote_gates() в promote flow (telegram.py).
        # Здесь цель: показать направление, не точное значение.
        g_n = n_terminal >= 40
        g_roi = sum_roi > 0
        g_sortino = sortino is not None and sortino > _SORTINO_THRESHOLD
        g_adv = adversarial_pass is True
        g_days = days_virtual >= 14
        g_exec = settings.execution_enabled
        gates_passed = sum([g_n, g_roi, g_sortino, g_adv, g_days, g_exec])

        blocker = _compute_blocker(
            n_terminal, avg_roi, sortino, days_virtual, adversarial_pass, settings.execution_enabled
        )

        rows.append(
            {
                "detector": det,
                "days_virtual": days_virtual,
                "n_terminal": n_terminal,
                "avg_roi_net": avg_roi,
                "sortino": sortino,
                "gates_passed": gates_passed,
                "gates_total": 6,
                "blocker": blocker,
                "resolved_pnl_usd": rd.get("pnl_resolved_usd"),
                "resolved_wins": rd.get("wins"),
                "resolved_losses": rd.get("losses"),
            }
        )

    rows.sort(key=lambda r: (r["blocker"] != "READY", -r["gates_passed"], -r["n_terminal"]))

    published = await publish(to_notifications(rows), actor="virtual_promote_report")

    logger.info(
        "virtual_promote_report_completed",
        n_virtual=len(rows),
        sent=published.sent,
        digested=published.digested,
        today=today,
    )
    return {"n_virtual": len(rows), "published": published}
