"""D46 calendar-spread scanner — накопление снапшотов цен для пар рынков одного
underlying/сущности с РАЗНЫМИ датами экспирации (research/fable-strategy-brief-2026-07-03/).

Гипотеза: P(early) <= P(late) — раннее событие логически влечёт позднее (crypto
threshold "by <date>") или это тот же кандидат/сущность на более раннем дедлайне
(politics). Нарушение монотонности = model-free арбитраж: купить NO(early) +
YES(late), суммарная цена < 1 гарантированно окупается при любом исходе (см. brief
§C1 в research/fable-strategy-brief-2026-07-03/response.md).

Gamma API не отдаёт готового group-поля для таких серий (verified 2026-07-03) —
матчинг через scripts/research/calendar_spread_matcher.py (regex на question/slug).
Готовой высокочастотной истории цен для произвольной пары рынков в БД нет
(daily_price_snapshots даёт только 1 точку/день) — поэтому этот скрипт сам
накапливает снапшоты periodic polling'ом, по образцу v2_market_snapshot.py.

Запуск (на VPS, нужен реальный сетевой доступ к Gamma):
    python -m scripts.research.d46_calendar_spread_scanner --interval 30m

Каждые --interval снимает текущую YES-цену всех найденных пар и дописывает строку
в JSONL. Анализ (проверка нарушений монотонности) — отдельным шагом после
накопления нескольких дней данных, см. analyze_snapshots().
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
from collections import defaultdict
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import structlog

from app.services.polymarket_service import PolymarketService
from scripts.research.calendar_spread_matcher import (
    ParsedMarket,
    ParsedPoliticsMarket,
    match_crypto_threshold,
    match_politics_series,
)

logger = structlog.get_logger()

OUT_DIR = Path(os.getenv("D46_SCANNER_OUT_DIR", "./research_out/d46_calendar_spread"))
GAMMA_PAGE_LIMIT = 100
MAX_PAGES = 20  # 2000 events — достаточно для активного crypto/politics срез, без нужды в полном dump


def _parse_outcome_yes_price(outcome_prices: Any) -> float | None:
    """Первая цена в outcomePrices = YES (mirror app/jobs/market_sync.py::parse_outcome_price,
    но возвращает None вместо 0.5 fallback — здесь фиктивная цена хуже отсутствия точки."""
    if not outcome_prices:
        return None
    if isinstance(outcome_prices, str):
        try:
            outcome_prices = json.loads(outcome_prices)
        except json.JSONDecodeError:
            return None
    if isinstance(outcome_prices, list) and outcome_prices:
        try:
            return float(outcome_prices[0])
        except (ValueError, TypeError):
            return None
    return None


async def fetch_active_markets(service: PolymarketService) -> list[dict[str, Any]]:
    """Разворачивает active events -> flat markets list (nested markets[] на событии)."""
    markets: list[dict[str, Any]] = []
    for page in range(MAX_PAGES):
        events = await service.fetch_active_events(limit=GAMMA_PAGE_LIMIT, offset=page * GAMMA_PAGE_LIMIT)
        if not events:
            break
        for event in events:
            markets.extend(event.get("markets") or [])
        if len(events) < GAMMA_PAGE_LIMIT:
            break
    return markets


def find_crypto_pairs(markets: list[dict[str, Any]]) -> dict[tuple[str, float], list[dict[str, Any]]]:
    """Группирует markets по (asset, strike) — серии с разными датами того же порога."""
    groups: dict[tuple[str, float], list[dict[str, Any]]] = defaultdict(list)
    for m in markets:
        question = m.get("question") or ""
        slug = m.get("slug") or ""
        parsed = match_crypto_threshold(question=question, slug=slug)
        if parsed is None:
            continue
        groups[(parsed.asset, parsed.strike)].append({**m, "_parsed": parsed})
    return {key: ms for key, ms in groups.items() if len(ms) >= 2}


def find_politics_pairs(markets: list[dict[str, Any]]) -> dict[str, list[dict[str, Any]]]:
    """Группирует markets по entity — серии одного кандидата/сущности на разных дедлайнах."""
    groups: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for m in markets:
        question = m.get("question") or ""
        slug = m.get("slug") or ""
        parsed = match_politics_series(question=question, slug=slug)
        if parsed is None:
            continue
        groups[parsed.entity].append({**m, "_parsed": parsed})
    return {key: ms for key, ms in groups.items() if len(ms) >= 2}


def _market_row(m: dict[str, Any], group_key: str) -> dict[str, Any] | None:
    yes_price = _parse_outcome_yes_price(m.get("outcomePrices"))
    if yes_price is None:
        return None
    parsed = m["_parsed"]
    end_date = parsed.end_date if isinstance(parsed, ParsedMarket | ParsedPoliticsMarket) else None
    return {
        "group_key": group_key,
        "condition_id": m.get("conditionId"),
        "slug": m.get("slug"),
        "question": m.get("question"),
        "end_date": end_date.isoformat() if end_date else None,
        "yes_price": yes_price,
        "volume_24h": m.get("volume24hr"),
    }


async def snapshot_once(service: PolymarketService) -> int:
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    ts = datetime.now(UTC)
    out_file = OUT_DIR / f"{ts.strftime('%Y%m%d')}.jsonl"

    markets = await fetch_active_markets(service)
    crypto_groups = find_crypto_pairs(markets)
    politics_groups = find_politics_pairs(markets)

    written = 0
    with out_file.open("a") as f:
        for (asset, strike), group_markets in crypto_groups.items():
            group_key = f"crypto:{asset}:{strike}"
            for m in group_markets:
                row = _market_row(m, group_key)
                if row is None:
                    continue
                f.write(json.dumps({"ts": ts.isoformat(), "kind": "crypto_threshold", **row}, ensure_ascii=False) + "\n")
                written += 1
        for entity, group_markets in politics_groups.items():
            group_key = f"politics:{entity}"
            for m in group_markets:
                row = _market_row(m, group_key)
                if row is None:
                    continue
                f.write(json.dumps({"ts": ts.isoformat(), "kind": "politics_series", **row}, ensure_ascii=False) + "\n")
                written += 1

    logger.info(
        "d46_scan_done",
        n_markets_scanned=len(markets),
        n_crypto_groups=len(crypto_groups),
        n_politics_groups=len(politics_groups),
        rows_written=written,
        file=str(out_file),
    )
    return written


async def loop(interval_seconds: int) -> None:
    service = PolymarketService()
    while True:
        try:
            await snapshot_once(service)
        except Exception as e:  # noqa: BLE001
            logger.error("d46_scan_error", error=str(e))
        await asyncio.sleep(interval_seconds)


def _parse_interval(s: str) -> int:
    if s.endswith("h"):
        return int(s[:-1]) * 3600
    if s.endswith("m"):
        return int(s[:-1]) * 60
    return int(s)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--interval", default="30m", help="e.g. 30m, 1h (default 30m — Gamma rate limit friendly)")
    parser.add_argument("--once", action="store_true", help="single snapshot, no loop")
    args = parser.parse_args()

    if args.once:
        asyncio.run(snapshot_once(PolymarketService()))
    else:
        asyncio.run(loop(_parse_interval(args.interval)))


if __name__ == "__main__":
    main()
