"""Detectors Honest Audit Report — еженедельный TG-монитор честного prod-PnL.

Считает гипотетический USD PnL по живым detector_signals (Wilson-style per-event
dedup), но ТОЛЬКО за свежее окно (default 30 дней) — это критично: без фильтра по
свежести аудит тащит killed-детекторы (D5/D7b/D30-33, файлов нет) и мёртвый backfill
(D_esports, d45) с фантомными «PROMOTE» вердиктами. Фильтр generated_at > now()-Nd
отсекает их сам, без хардкод-списка, который протухает.

Это forward-only «термометр» деградации детекторов по ЖИВОМУ потоку — НЕ canonical
бэктест (тот систематически переоценивает 5-10× через volume-proxy, см.
[[strategy-edge-not-data-2026-06-15]]). Источник правды для promote — promote_gates.py
(тоже live-only). Этот отчёт — мониторинг, не gate.

Cron: еженедельно (воскресенье), после ml-fit кранов.
Dedup: top-1 per (detector, event_id) by created_at.
Stake assumption: $25/trade (0.5% от $5K). realized_roi_net хранится в ПРОЦЕНТАХ.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

import structlog

from app.analytics.notification import Notification, Severity
from app.analytics.subject_labels import humanize_subject
from app.services.database import fetch
from app.services.notification_bus import publish

logger = structlog.get_logger(__name__)

DEFAULT_WINDOW_DAYS = 30
_STAKE_USD = 25.0  # 0.5% от $5K bankroll (MAX_STAKE_PCT_DEFAULT)
_INSUFFICIENT_N = 100
_TREND_DELTA_THRESHOLD = 1.0  # $/day @ $1K — порог «заметного» сдвига между половинами окна

# realized_roi_net в ПРОЦЕНТАХ (-100 = -100%), нормируем /100 → proportion.
# Свежесть: только generated_at за окно N дней ($1) — отсекает killed/backfill автоматически.
# Группировка (detector, category): LEFT JOIN markets, NULL-категория → 'unknown'.
# Тренд: суммы ROI и счётчики раздельно для первой/второй половины окна.
# midpoint = now() - (окно)/2: делим готовый interval на 2, чтобы $1 везде остался голым int
# (без ::float-каста — иначе asyncpg мог бы вывести конфликтующий тип для двух вхождений $1).
_AUDIT_SQL = """
WITH per_signal AS (
    SELECT
        s.detector,
        COALESCE(m.category, 'unknown') AS category,
        s.event_id,
        s.realized_roi_net,
        s.generated_at,
        s.created_at,
        (s.generated_at > now() - make_interval(days => $1) / 2) AS is_second_half,
        ROW_NUMBER() OVER (
            PARTITION BY s.detector, s.event_id
            ORDER BY s.created_at
        ) AS event_rank
    FROM detector_signals s
    LEFT JOIN markets m ON m.polymarket_id = s.market_id
    WHERE s.resolved = true
      AND s.realized_roi_net IS NOT NULL
      AND s.event_id IS NOT NULL
      AND s.generated_at > now() - make_interval(days => $1)
)
SELECT
    detector,
    category,
    COUNT(*) FILTER (WHERE event_rank = 1) AS n_dedup,
    SUM(realized_roi_net) FILTER (WHERE event_rank = 1) / 100.0 AS sum_roi_prop,
    COUNT(*) FILTER (WHERE event_rank = 1 AND NOT is_second_half) AS n_first_half,
    SUM(realized_roi_net) FILTER (WHERE event_rank = 1 AND NOT is_second_half) / 100.0 AS sum_roi_prop_first,
    COUNT(*) FILTER (WHERE event_rank = 1 AND is_second_half) AS n_second_half,
    SUM(realized_roi_net) FILTER (WHERE event_rank = 1 AND is_second_half) / 100.0 AS sum_roi_prop_second,
    MIN(generated_at) FILTER (WHERE event_rank = 1)::date AS first_signal,
    MAX(generated_at) FILTER (WHERE event_rank = 1)::date AS last_signal
  FROM per_signal
 GROUP BY detector, category
HAVING COUNT(*) FILTER (WHERE event_rank = 1) > 0
"""


def to_notifications(
    rows: list[dict[str, Any]], days: int, live_detectors: set[str] | None = None
) -> list[Notification]:
    """Еженедельный честный аудит → Notification (спека §3, §4).

    Severity по факту: отчёт шлётся по расписанию независимо от результата и по
    §4 это FYI, но связка с вердиктом KILL у ЖИВОГО детектора — состояние с
    действием (деньги уходят прямо сейчас). Увести такое в дайджест значит
    потерять решение (риск №4 §10).

    `live_detectors` — те, кто реально торгует деньгами (stage='live').
    Прод-дефект 2026-08-04: без этого параметра сообщение утверждало «теряет
    $39 в день» и «забирает $1180 в месяц» про M2 (shadow) и D36 (paused) —
    детекторы, не торгующие деньгами вообще. Цифра не выдумана, но подана как
    текущий убыток, хотя это исторический счёт по сигналам; владелец пошёл бы
    отключать уже отключённое. §3 запрещает не только выдуманные числа, но и
    настоящие числа, поданные как то, чем они не являются.

    what_if_ignored считается из фактического `per_day` (при stake $25) и
    ТОЛЬКО по живым связкам. Приводится к месяцу: суточная цифра не читается
    как решение.
    """
    if not rows:
        return []

    live = live_detectors or set()
    kills = [r for r in rows if "KILL" in r["verdict"]]

    if not kills:
        promote = [r for r in rows if "PROMOTE" in r["verdict"]]
        lines = [f"Проверено связок детектор+категория: {len(rows)} за {days} дней."]
        if promote:
            names = ", ".join(f"{r['detector']}/{r['category']}" for r in promote[:3])
            lines.append(f"Кандидаты на продвижение: {names}.")
        else:
            lines.append("Ни одна связка не показала устойчивого преимущества и не требует вмешательства.")

        return [
            Notification(
                severity=Severity.FYI,
                subject="analytics:honest_audit",
                kind="honest_audit_report",
                headline="Честный аудит детекторов за неделю",
                what_happened=lines,
                what_if_ignored=None,
                what_to_do=None,
                actions=[],
                evidence=[
                    f"{r['detector']}/{r['category']}: {r['verdict']}, n={r['n_dedup']}, ${r['per_day']:.2f}/день"
                    for r in rows
                ],
            )
        ]

    live_kills = [r for r in kills if r["detector"] in live]

    lines = []
    for r in sorted(kills, key=lambda x: x["per_day"]):
        name = humanize_subject("detector:" + r["detector"])
        verb = "теряет" if r["detector"] in live else "терял бы"
        lines.append(
            f"{name} на «{r['category']}»: {verb} ${abs(r['per_day']):.2f} в день на выборке из {r['n_dedup']} сделок."
        )
    lines.append("Выборка достаточная, чтобы считать это устойчивым результатом, а не полосой невезения.")

    # Денежная строка — только по ЖИВЫМ связкам и только от худшей из них.
    # Худшая вообще может оказаться shadow-детектором и задрать сумму до цифры,
    # которой в реальности не существует (прод-дефект 2026-08-04: $1180/мес
    # приписаны M2 в shadow и D36 в paused).
    cost = None
    if live_kills:
        worst_live = min(live_kills, key=lambda r: r["per_day"])
        monthly = abs(worst_live["per_day"]) * 30
        cost = (
            f"Худшая из работающих связок забирает около ${monthly:.0f} в месяц "
            "при текущем размере ставки. Это расчёт по фактическим сделкам, не прогноз."
        )
    else:
        lines.append(
            "Ни один из них сейчас не торгует настоящими деньгами — это счёт по истории сигналов, а не текущий убыток."
        )

    return [
        Notification(
            # Нет живых среди убыточных — нет срочности: ACTION_REQUIRED каждую
            # неделю про уже выключенное быстро обесценивает сам уровень (§4).
            severity=Severity.ACTION_REQUIRED if live_kills else Severity.FYI,
            subject="analytics:honest_audit",
            kind="honest_audit_kill",
            headline=(
                f"Связок, стабильно теряющих деньги: {len(live_kills)}"
                if live_kills
                else f"Убыточных связок в истории: {len(kills)} (деньгами не торгуют)"
            ),
            what_happened=lines,
            what_if_ignored=cost,
            what_to_do=(
                (
                    "Отключи эти связки детектор+категория или разберись, почему они убыточны. "
                    "Автоматически ничего не отключено."
                )
                if live_kills
                else None
            ),
            actions=[],
            evidence=[
                f"{r['detector']}/{r['category']}: {r['verdict']}, n={r['n_dedup']}, "
                f"${r['per_day']:.2f}/день, ${r['per_day_1k']:.2f}/день@$1K, {r['trend']}"
                for r in kills
            ],
        )
    ]


# Строго 'live': shadow/virtual деньгами не торгуют, и убыток по ним —
# исторический счёт по сигналам, а не потеря (прод-дефект 2026-08-04).
_LIVE_DETECTORS_SQL = "SELECT detector FROM detector_state WHERE stage = 'live'"


def _verdict(n_dedup: int, per_day_1k: float) -> str:
    """Вердикт по decision-matrix (n приоритетнее PnL).

    n_dedup < 100 → INSUFFICIENT; иначе по $/day @ $1K bankroll:
    < -2 KILL, -2..+5 MARGINAL, > +5 PROMOTE candidate.
    """
    if n_dedup < _INSUFFICIENT_N:
        return f"⏸️ INSUFFICIENT (n&lt;{_INSUFFICIENT_N})"
    if per_day_1k < -2.0:
        return "❌ KILL"
    if per_day_1k > 5.0:
        return "✅ PROMOTE candidate"
    return "⚠️ MARGINAL"


def _trend_label(first_half_per_day_1k: float | None, second_half_per_day_1k: float | None) -> str:
    """Метка тренда деградации по двум половинам окна.

    Сравниваем per_day_1k второй половины с первой. Сдвиг ≥ _TREND_DELTA_THRESHOLD
    вниз → 📉 (edge уезжает, как live_brier у ML), вверх → 📈, иначе → ➡️.
    None в любой половине (нет резолвов) → пустая метка (нечего сравнивать).
    """
    if first_half_per_day_1k is None or second_half_per_day_1k is None:
        return ""
    delta = second_half_per_day_1k - first_half_per_day_1k
    if delta <= -_TREND_DELTA_THRESHOLD:
        return "📉"
    if delta >= _TREND_DELTA_THRESHOLD:
        return "📈"
    return "➡️"


def _half_per_day_1k(sum_roi_prop: float, half_days: float) -> float | None:
    """per_day_1k для половины окна. half_days≤0 → None (нет интервала для нормировки)."""
    if half_days <= 0:
        return None
    pnl_usd = sum_roi_prop * _STAKE_USD
    per_day = pnl_usd / half_days
    return per_day / 5.0  # $5K → $1K bankroll


def _format_line(row: dict[str, Any]) -> str:
    """Одна строка отчёта по (детектору, категории) с trend-меткой."""
    category = row.get("category", "")
    trend = row.get("trend", "")
    cat_part = f" [{category}]" if category else ""
    trend_part = f" {trend}" if trend else ""
    return (
        f"{row['verdict']} {row['detector']}{cat_part}{trend_part} | "
        f"n={row['n_dedup']:,} | "
        f"${row['per_day']:+,.1f}/day @$5K | "
        f"${row['per_day_1k']:+.1f}/day @$1K"
    )


def _format_message(rows: list[dict[str, Any]], today: str, days: int) -> str:
    """Собрать полное TG-сообщение (sorted: по детектору, внутри — per_day_1k desc)."""
    lines = [
        f"🌡 <b>Detectors Honest Audit — {today}</b>",
        f"forward-only по живым detector_signals за {days}д (dedup per-event, $25 stake, [категория] + тренд)",
        "",
    ]
    if not rows:
        lines.append(f"нет свежих resolved-сигналов за {days}д")
        return "\n".join(lines)

    # Сортировка: сначала по детектору (чтобы категории одного детектора шли рядом),
    # внутри детектора — по per_day_1k desc (худшая категория ниже).
    ordered = sorted(rows, key=lambda r: (r["detector"], -r["per_day_1k"]))
    for row in ordered:
        lines.append(_format_line(row))

    promote = sum(1 for r in rows if "PROMOTE" in r["verdict"])
    kill = sum(1 for r in rows if "KILL" in r["verdict"])
    lines.extend(
        [
            "",
            f"PROMOTE-кандидатов: {promote} · KILL: {kill} · всего: {len(rows)}",
            "⚠️ Это термометр по ЖИВОМУ потоку, НЕ canonical-бэктест и НЕ promote-gate.",
        ]
    )
    lines.extend(_WEEKLY_CHECKLIST)
    return "\n".join(lines)


# Еженедельный чеклист месяца «честная проверка edge» — добавляется в TG-отчёт,
# чтобы сообщение само напоминало ритм. См. strategy_edge_not_data_2026-06-15.
_WEEKLY_CHECKLIST = [
    "",
    "— — — ритм месяца — — —",
    "✅ ПРОВЕРЬ: есть ли PROMOTE-кандидат с n≥100, стабильный 2-3 недели?",
    "✅ ГЛЯНЬ: ML-badge в Insights (L2/Layer A всё ещё 🔒 FROZEN — ожидаемо).",
    "📝 ЗАПИШИ строкой: неделя · сколько PROMOTE · лучший детектор.",
    "🚫 НЕ ДЕЛАЙ: не тюнь детекторы, не вкл. EXECUTION_ENABLED, не смотри canonical-бэктест.",
    "🗓 Чистый вердикт — после 20 июня (окно станет 100% пост-майским).",
]


async def run_detectors_honest_audit_report(days: int = DEFAULT_WINDOW_DAYS) -> dict[str, Any]:
    """Собрать honest prod-PnL по живым детекторам за окно `days` и отправить через шину."""
    today = datetime.now(UTC).strftime("%Y-%m-%d")

    db_rows = await fetch(_AUDIT_SQL, days)
    half_days = days / 2.0
    rows: list[dict[str, Any]] = []
    for r in db_rows:
        n_dedup = int(r["n_dedup"] or 0)
        sum_roi_prop = float(r["sum_roi_prop"] or 0)
        first = r["first_signal"]
        last = r["last_signal"]
        span = max((last - first).days, 1) if first and last else 1
        pnl_usd = sum_roi_prop * _STAKE_USD
        per_day = pnl_usd / span
        per_day_1k = per_day / 5.0  # $5K → $1K bankroll

        # Намеренная асимметрия знаменателя: вердикт (per_day_1k выше) нормируется на span
        # (фактический диапазон дат сигналов), тренд — на фиксированный half_days. Это ОК:
        # тренд сравнивает половины МЕЖДУ СОБОЙ при общем знаменателе half_days — он сокращается,
        # знак delta зависит только от сумм ROI половин. Менять вердикт на calendar-дни нельзя —
        # сдвинет существующие KILL/PROMOTE всех детекторов (вне scope термометра тренда).
        first_half_pd1k = (
            _half_per_day_1k(float(r["sum_roi_prop_first"] or 0), half_days)
            if int(r["n_first_half"] or 0) > 0
            else None
        )
        second_half_pd1k = (
            _half_per_day_1k(float(r["sum_roi_prop_second"] or 0), half_days)
            if int(r["n_second_half"] or 0) > 0
            else None
        )
        rows.append(
            {
                "detector": r["detector"],
                "category": r["category"],
                "n_dedup": n_dedup,
                "per_day": per_day,
                "per_day_1k": per_day_1k,
                "trend": _trend_label(first_half_pd1k, second_half_pd1k),
                "verdict": _verdict(n_dedup, per_day_1k),
            }
        )

    # Строго stage='live' — не shadow/virtual: только эти детекторы реально
    # тратят деньги, и только про них можно говорить «теряет» в настоящем времени.
    live_rows = await fetch(_LIVE_DETECTORS_SQL)
    live_detectors = {r["detector"] for r in live_rows}

    published = await publish(
        to_notifications(rows, days, live_detectors=live_detectors),
        actor="detectors_honest_audit_report",
    )

    logger.info(
        "honest_audit_report_completed",
        n_detectors=len(rows),
        days=days,
        sent=published.sent,
        digested=published.digested,
        today=today,
    )
    return {"n_detectors": len(rows), "days": days, "published": published}
