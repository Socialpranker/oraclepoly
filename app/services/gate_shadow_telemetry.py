"""Shadow-телеметрия для гейтов на пересмотре (по образцу price_staleness_telemetry).

Ничего не меняет в решениях — только измеряет. Фиксирует reject + книгу ордеров на
момент сигнала; честный "would have filled" PnL считается позже отдельным SQL-join
к detector_signals.outcome/realized_roi_net по detector_signal_id (не здесь).
Первый потребитель: category_blacklist:Sports на D3_cross_mkt_arb
(research/fable-gates-audit-2026-07-03/), затем human decision об исключении D3
из _CATEGORY_BLACKLIST (n>=30 resolved, ROI>0).
"""

from typing import Any

import structlog

from app.services.database import execute
from app.services.execution_safety import fetch_live_book

logger = structlog.get_logger()

_INSERT_SQL = """
INSERT INTO gate_shadow_log
    (detector, gate, detector_signal_id, stake_usd, best_bid, best_ask, depth_usd)
VALUES ($1, $2, $3, $4, $5, $6, $7)
ON CONFLICT (detector, gate, detector_signal_id) DO NOTHING
"""


async def log_gate_shadow(
    detector: str,
    gate: str,
    detector_signal_id: Any,
    stake_usd: float | None = None,
    token_id: str | None = None,
) -> None:
    """Залогировать reject гейта + книгу на момент сигнала (best-effort).

    Книга нужна для честного fill-realism при последующем анализе (реальный fill
    сквозь спред, не по signal-цене). Best-effort: fetch_live_book WS-first,
    REST-fallback — при сбое пишем NULL-книгу, не блокируя телеметрию.
    Никогда не бросает — телеметрия не роняет money-path.
    """
    best_bid = best_ask = depth_usd = None
    if token_id:
        try:
            book = await fetch_live_book(token_id)
            if book:
                best_bid = round(book["bid"], 6)
                best_ask = round(book["ask"], 6)
                depth_usd = round(book["bid_size"] * book["bid"] + book["ask_size"] * book["ask"], 2)
        except Exception as exc:  # noqa: BLE001 — best-effort, не роняет телеметрию
            logger.debug("gate_shadow_book_fetch_failed", detector=detector, gate=gate, error=str(exc))

    try:
        await execute(
            _INSERT_SQL,
            detector,
            gate,
            detector_signal_id,
            round(stake_usd, 2) if stake_usd is not None else None,
            best_bid,
            best_ask,
            depth_usd,
        )
    except Exception as exc:  # noqa: BLE001 — телеметрия не роняет money-path
        logger.debug("gate_shadow_log_failed", detector=detector, gate=gate, error=str(exc))
