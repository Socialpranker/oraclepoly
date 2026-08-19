"""
V2 Migration baseline snapshot — orderbook depth/spread/liquidity по топ-N рынкам.

Цель: собрать post-V2 данные сразу после миграции (28 апр 2026), сравнить с pre-V2
baseline'ом из oracle_snapshots/trade_signals. Hypothesis H2: composition shift даёт
edge-окно в первые 14 дней (часть bot-MM отвалилась, не успев интегрировать V2 SDK).

Запуск (на VPS):
    python -m scripts.research.v2_market_snapshot --top 50 --interval 6h

Каждые 6 часов снимает orderbook'и топ-N активных рынков и пишет в JSONL на /mnt/data.

Метрики per-market:
    spread_bps, mid, depth_1pct, depth_5pct, levels_count, vol_24h, category, era_tag.

Анализ через DuckDB после 7-14 дней накопления.
"""

import argparse
import asyncio
import json
import os
from datetime import UTC, datetime
from pathlib import Path

import httpx
import structlog

from app.services.polymarket_poller_service import PolymarketPollerService

logger = structlog.get_logger()

GAMMA_MARKETS = "https://gamma-api.polymarket.com/markets"
# Конфигурируется через env. Дефолт работает без extra mount'ов.
OUT_DIR = Path(os.getenv("V2_SNAPSHOT_OUT_DIR", "./research_out/v2_snapshots"))
ERA_TAG = "post_v2"


async def fetch_top_markets(limit: int) -> list[dict]:
    async with httpx.AsyncClient(timeout=15.0) as c:
        r = await c.get(
            GAMMA_MARKETS,
            params={"closed": "false", "active": "true", "limit": limit, "order": "volume24hr", "ascending": "false"},
        )
        r.raise_for_status()
        return r.json()


def _depth_within(levels: list[dict], mid: float, pct: float) -> float:
    lo, hi = mid * (1 - pct), mid * (1 + pct)
    return sum(float(lvl["size"]) for lvl in levels if lo <= float(lvl["price"]) <= hi)


def _book_metrics(book: dict, vol_24h: float) -> dict | None:
    bids, asks = book.get("bids") or [], book.get("asks") or []
    if not bids or not asks:
        return None
    best_bid = float(bids[0]["price"])
    best_ask = float(asks[0]["price"])
    if best_ask <= best_bid:
        return None
    mid = (best_bid + best_ask) / 2
    return {
        "best_bid": best_bid,
        "best_ask": best_ask,
        "mid": round(mid, 6),
        "spread_bps": round((best_ask - best_bid) / mid * 10_000, 2),
        "depth_1pct": _depth_within(bids, mid, 0.01) + _depth_within(asks, mid, 0.01),
        "depth_5pct": _depth_within(bids, mid, 0.05) + _depth_within(asks, mid, 0.05),
        "levels_bid": len(bids),
        "levels_ask": len(asks),
        "vol_24h": vol_24h,
    }


async def snapshot_once(top: int) -> int:
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    ts = datetime.now(UTC)
    out_file = OUT_DIR / f"{ts.strftime('%Y%m%d')}.jsonl"

    markets = await fetch_top_markets(top)
    poller = PolymarketPollerService()
    written = 0

    for m in markets:
        clob_ids_raw = m.get("clobTokenIds") or "[]"
        try:
            token_ids = json.loads(clob_ids_raw) if isinstance(clob_ids_raw, str) else clob_ids_raw
        except (json.JSONDecodeError, TypeError):
            continue
        if not token_ids:
            continue

        for outcome_idx, token_id in enumerate(token_ids):
            book = await poller.fetch_order_book_rest(str(token_id))
            if not book:
                continue
            metrics = _book_metrics(book, float(m.get("volumeNum") or 0))
            if not metrics:
                continue

            row = {
                "ts": ts.isoformat(),
                "era": ERA_TAG,
                "condition_id": m.get("conditionId"),
                "slug": m.get("slug"),
                "category": m.get("category"),
                "outcome_idx": outcome_idx,
                "outcome_name": (m.get("outcomes") or ["YES", "NO"])[outcome_idx]
                if outcome_idx < len(m.get("outcomes") or [])
                else None,
                "token_id": str(token_id),
                **metrics,
            }
            with out_file.open("a") as f:
                f.write(json.dumps(row, ensure_ascii=False) + "\n")
            written += 1

    logger.info("v2_snapshot_done", markets=len(markets), rows=written, file=str(out_file))
    return written


async def loop(top: int, interval_seconds: int) -> None:
    while True:
        try:
            await snapshot_once(top)
        except Exception as e:
            logger.error("v2_snapshot_error", error=str(e))
        await asyncio.sleep(interval_seconds)


def _parse_interval(s: str) -> int:
    if s.endswith("h"):
        return int(s[:-1]) * 3600
    if s.endswith("m"):
        return int(s[:-1]) * 60
    return int(s)


if __name__ == "__main__":
    p = argparse.ArgumentParser()
    p.add_argument("--top", type=int, default=50)
    p.add_argument("--interval", type=str, default="6h", help="6h|30m|3600")
    p.add_argument("--once", action="store_true")
    args = p.parse_args()

    if args.once:
        asyncio.run(snapshot_once(args.top))
    else:
        asyncio.run(loop(args.top, _parse_interval(args.interval)))
