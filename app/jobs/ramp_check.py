"""Daily-проверка ramp-лесенки (Phase 3 Task 2, audit research-gap).

Канареечный размер live-ставки раньше держался только "на бумаге"
(docs/superpowers/plans/2026-06-25-d3-live-proxy-ramp.md §3.1) и молчаливо
переставал защищать банк при пополнении кошелька — сам код нигде не
энфорсил потолок. Теперь потолок живёт в detector_state.live_stake_cap_usd
(см. app/core/detector_state.py) и применяется в v2_execution._process_signal.

Эта джоба ежедневно проверяет: для каждого live-детектора с заданным
cap — выполнены ли условия перехода на следующую ступень лесенки
(§3.1: резолвнутые, НЕ early-exit ставки, resolved-PnL >= 0). Если да —
шлёт TG-кнопку "поднять cap" (human-gated, ни один cap не меняется
автоматически). Debounce раз в сутки per detector — джоба сама daily,
но защищаемся от повторных ручных прогонов в тот же день.

Ступени — ТОЧНЫЕ числа из ramp-плана §3.1 (не выдуманные placeholder'ы):
  S0 канарейка: cap $10 (верхняя граница диапазона $5-10)
  S1:           cap $20 (верхняя граница диапазона $15-20)
  S2:           cap $40 (верхняя граница диапазона $30-40)
  S3 «полный»:  next_cap=None — по EV-sizer cap, ramp-план НЕ даёт фиксированного
                числа для этой ступени ("держим, мониторим"). Джоба не предлагает
                поднять cap дальше $40 — снятие потолка (переход на EV-sizer cap
                без ramp-ограничения) остаётся human decision вне этой лесенки.

Условия перехода (§3.1, gating по РЕЗОЛВ-PnL, не paper):
  S0→S1: >=10 резолвнутых (не early-exit) ставок, resolved-PnL >= 0
  S1→S2: >=20 резолвов (условие Wilson-WR lower >= break-even — не считаем
         здесь, т.к. требует полного calibration стека; используем min_pnl>=0
         как консервативный прокси, оставляя более сильный статистический
         гейт человеку при подтверждении через TG)
  S2→S3: >=30 резолвов, min_pnl>=0 (Sortino/catastrophic-гейты — human review)
"""

from typing import Any

import structlog

from app.core.task_queue import debounce_acquire
from app.services.database import fetch

logger = structlog.get_logger()

# Ступени ramp-лесенки — см. docstring выше и ramp-план §3.1. cap=None
# означает "без потолка" (нет смысла матчить, т.к. detector_state
# для live-детекторов без cap этой джобой не рассматривается).
RAMP_LADDER: list[dict[str, Any]] = [
    {"cap": 10.0, "next_cap": 20.0, "min_resolved": 10, "min_pnl": 0.0},
    {"cap": 20.0, "next_cap": 40.0, "min_resolved": 20, "min_pnl": 0.0},
    {"cap": 40.0, "next_cap": None, "min_resolved": 30, "min_pnl": 0.0},
]

_LIVE_DETECTORS_WITH_CAP_SQL = """
SELECT detector, live_stake_cap_usd
  FROM detector_state
 WHERE stage = 'live' AND live_stake_cap_usd IS NOT NULL
"""

# Резолвнутые (НЕ early-exit) live-ставки: §3.1 требует gating по РЕЗОЛВ-PnL,
# early-exit фактический PnL может не совпасть с paper-ожиданием (slippage).
_RESOLVED_LIVE_SQL = """
SELECT COUNT(*) AS n, COALESCE(SUM(net_pnl), 0) AS pnl
  FROM execution_orders
 WHERE detector = $1 AND execution_mode IN ('maker', 'taker')
   AND status = 'settled' AND outcome != 'EARLY_EXIT'
"""

_DEBOUNCE_TTL_SEC = 86400  # раз в сутки per detector


def _find_rung(cap: float) -> dict[str, Any] | None:
    """Найти ступень лесенки по текущему cap. None если cap не матчит ни одну ступень
    (например уже выше S2, или кастомное значение, выставленное вручную)."""
    for rung in RAMP_LADDER:
        if abs(rung["cap"] - cap) < 0.01:
            return rung
    return None


async def run_ramp_check() -> dict[str, Any]:
    """Cron entry point. Daily.

    Returns dict: {"detectors_checked": int, "promotions_offered": int}.
    """
    rows = await fetch(_LIVE_DETECTORS_WITH_CAP_SQL)
    checked = 0
    offered = 0

    for row in rows:
        detector = row["detector"]
        cap = float(row["live_stake_cap_usd"])
        checked += 1

        rung = _find_rung(cap)
        if rung is None or rung["next_cap"] is None:
            continue  # cap не на известной ступени, либо уже на верхней (S3)

        stats_row = await fetch(_RESOLVED_LIVE_SQL, detector)
        n = int(stats_row[0]["n"]) if stats_row else 0
        pnl = float(stats_row[0]["pnl"]) if stats_row else 0.0

        if n < rung["min_resolved"] or pnl < rung["min_pnl"]:
            continue

        if not await debounce_acquire(f"ramp_check:{detector}", _DEBOUNCE_TTL_SEC):
            logger.info("ramp_check_debounced", detector=detector, cap=cap)
            continue

        await _offer_ramp_up(detector, cap, rung["next_cap"], n, pnl)
        offered += 1

    logger.info("ramp_check_done", detectors_checked=checked, promotions_offered=offered)
    return {"detectors_checked": checked, "promotions_offered": offered}


async def _offer_ramp_up(detector: str, cap: float, next_cap: float, n: int, pnl: float) -> None:
    from app.tasks.notifications import send_admin_message_with_keyboard

    text = (
        f"📈 <b>{detector}</b>: ступень ramp пройдена "
        f"(n={n} резолвов, pnl={pnl:+.2f})\n"
        f"Поднять cap ${cap:.0f} → ${next_cap:.0f}?"
    )
    buttons = [{"text": f"⬆️ ${cap:.0f} → ${next_cap:.0f}", "callback_data": f"ramp_up:{detector}:{next_cap}"}]
    await send_admin_message_with_keyboard(text, buttons)
