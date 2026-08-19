"""Shadow-телеметрия расхождения db-цены и WS live-цены в денежных точках.

Ничего не меняет в решениях — только измеряет. Гейт-анализ 3-7 дней решает,
строить ли event-driven детекцию (План 2).
План: docs/superpowers/plans/2026-07-03-event-driven-price-pipeline.md
"""

from typing import Literal

import structlog

from app.services.database import execute
from app.services.live_prices import get_live_yes_price

logger = structlog.get_logger()

DecisionPoint = Literal["sizing", "bucket_gate", "mtm"]

_INSERT_SQL = """
INSERT INTO price_staleness_log
    (decision_point, market_id, yes_token_id, db_price, live_price, is_live, delta_abs)
VALUES ($1, $2, $3, $4, $5, $6, $7)
"""


async def log_price_staleness(
    point: DecisionPoint,
    market_id: str,
    yes_token_id: str | None,
    db_price: float,
) -> None:
    """Сравнить db_price (цена, по которой принято решение) с WS-midpoint сейчас.

    db_price для 'sizing' — снапшот на момент детекции: delta измеряет ПОЛНОЕ
    устаревание (возраст снапшота + лаг детекция→исполнение) — именно то, что
    стоит денег в Kelly-сайзинге. Никогда не бросает — телеметрия не роняет money-path.
    """
    try:
        live_price, is_live = await get_live_yes_price(yes_token_id, db_price)
        await execute(
            _INSERT_SQL,
            point,
            market_id,
            yes_token_id,
            round(db_price, 6),
            round(live_price, 6) if is_live else None,
            is_live,
            round(abs(live_price - db_price), 6) if is_live else None,
        )
    except Exception as exc:  # noqa: BLE001 — телеметрия не роняет money-path
        logger.debug("price_staleness_log_failed", point=point, market=market_id, error=str(exc))
