"""NegRisk Sync — каждые 30 минут обогащает events neg-risk флагами.

Без этих полей D3 CrossMktArb детектор не активен (по умолчанию neg_risk=false).

Запросы БЕЗ прокси — read-only Gamma API. Proxy допустим только в execution.

Поля:
    neg_risk        — Polymarket пометил event как neg-risk (mutually exclusive)
    neg_risk_other  — есть catch-all market ("Other"/"None of the above")
                      Если true — sum(YES) не обязан = 1.0, arb-сигнал ложный
    n_yes_winners   — сколько YES-исходов разрешаются победителем одновременно
                      Для D3 нужны только события с n_yes_winners = 1
"""

from __future__ import annotations

from typing import Any

import structlog

from app.services.database import execute, fetch
from app.services.polymarket_service import get_polymarket_service

logger = structlog.get_logger()

PAGE_LIMIT = 100
MAX_PAGES = 10


def _is_other_market(market: dict[str, Any]) -> bool:
    """Эвристика: market — catch-all ('Other', 'None of the above').

    Polymarket Gamma не всегда возвращает явный флаг, проверяем по полям:
    - groupItemTitle == 'Other'
    - question / slug содержит 'none of the above' / 'neither' / явное 'other'
    """
    group_title = (market.get("groupItemTitle") or "").strip().lower()
    if group_title == "other":
        return True

    def _norm(s: str | None) -> str:
        return " " + " ".join((s or "").lower().replace("-", " ").replace("_", " ").split()) + " "

    text = _norm(market.get("question")) + _norm(market.get("slug"))
    needles = ("none of the above", " neither ")
    if any(n in text for n in needles):
        return True
    if " some other " in text:
        return True
    return False


def _extract_metadata(event: dict[str, Any]) -> dict[str, Any] | None:
    """Извлекает neg-risk поля из event response.

    Возвращает None если event не активен или нет polymarket_id.
    """
    polymarket_id = event.get("id")
    if not polymarket_id:
        return None

    neg_risk = bool(event.get("negRisk"))
    markets = event.get("markets") or []
    neg_risk_other = any(_is_other_market(m) for m in markets) if neg_risk else False
    n_yes_winners = 1 if neg_risk else max(1, int(event.get("nYesWinners", 1) or 1))

    return {
        "polymarket_id": str(polymarket_id),
        "neg_risk": neg_risk,
        "neg_risk_other": neg_risk_other,
        "n_yes_winners": n_yes_winners,
    }


async def _persist(metadata: list[dict[str, Any]]) -> int:
    """UPDATE events по batch'у. Возвращает число обновлённых строк."""
    if not metadata:
        return 0

    sql = """
    UPDATE events
       SET neg_risk = data.neg_risk,
           neg_risk_other = data.neg_risk_other,
           n_yes_winners = data.n_yes_winners,
           updated_at = now()
      FROM (
        SELECT
          unnest($1::text[])    AS polymarket_id,
          unnest($2::boolean[]) AS neg_risk,
          unnest($3::boolean[]) AS neg_risk_other,
          unnest($4::int[])     AS n_yes_winners
      ) AS data
     WHERE events.polymarket_id = data.polymarket_id
       AND (
            events.neg_risk       IS DISTINCT FROM data.neg_risk OR
            events.neg_risk_other IS DISTINCT FROM data.neg_risk_other OR
            events.n_yes_winners  IS DISTINCT FROM data.n_yes_winners
       )
    """
    polymarket_ids = [m["polymarket_id"] for m in metadata]
    neg_risk = [m["neg_risk"] for m in metadata]
    neg_risk_other = [m["neg_risk_other"] for m in metadata]
    n_yes_winners = [m["n_yes_winners"] for m in metadata]

    await execute(sql, polymarket_ids, neg_risk, neg_risk_other, n_yes_winners)

    rows = await fetch(
        "SELECT COUNT(*) AS n FROM events WHERE neg_risk = true AND polymarket_id = ANY($1)",
        polymarket_ids,
    )
    return int(rows[0]["n"]) if rows else 0


async def run_negrisk_sync() -> dict[str, Any]:
    """Cron entry-point.

    Steps:
      1. Fetch active events (paginated, max 1000)
      2. Extract neg-risk metadata
      3. UPDATE events (batch SQL, only when changed)
    """
    poly_service = get_polymarket_service()

    all_events: list[dict[str, Any]] = []
    for page_idx in range(MAX_PAGES):
        page = await poly_service.fetch_active_events(
            limit=PAGE_LIMIT,
            offset=page_idx * PAGE_LIMIT,
        )
        if not page:
            break
        all_events.extend(page)
        if len(page) < PAGE_LIMIT:
            break

    fetched = len(all_events)
    if fetched == 0:
        logger.warning("negrisk_sync_empty_fetch")
        return {"fetched": 0, "extracted": 0, "neg_risk_active": 0}

    metadata = [m for m in (_extract_metadata(e) for e in all_events) if m is not None]
    extracted = len(metadata)

    neg_risk_active = await _persist(metadata)

    logger.info(
        "negrisk_sync_completed",
        fetched=fetched,
        extracted=extracted,
        neg_risk_active=neg_risk_active,
    )

    return {
        "fetched": fetched,
        "extracted": extracted,
        "neg_risk_active": neg_risk_active,
    }
