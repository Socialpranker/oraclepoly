"""
NegRisk arbitrage scanner — поиск событий где Σ(YES_prices) ≠ $1.00 в neg-risk markets.

Базовая идея: в neg-risk event ровно один outcome резолвится в YES → Σ всех YES = $1 при резолве.
Если рынок котирует Σ < $1 — buy-arb (купить все YES, получить $1).
Если Σ > $1 — sell-arb (продать все YES или конвертировать через NegRiskAdapter).

⚠️ Critical methodology — учитывать catch-all "Other" market:
  Polymarket добавляет к большинству neg-risk events маркет с флагом `negRiskOther: True`.
  Если он `active: False` (не торгуется) — sum_yes по quoted маркетам < 1, но это
  НЕ арбитраж: Other структурно покрывает residual.
  Реальный арбитраж — только если в event НЕТ catch-all (`has_other == False`).

Pipeline:
  1. Gamma /events?closed=false&limit=200 → отфильтровать negRisk=true
  2. Для каждого: classify markets как regular vs negRiskOther
  3. Compute sum_quoted = Σ outcomePrices[YES] для active regular markets
  4. Validation step (если sum_quoted в edge zone): для каждого active market забрать
     CLOB orderbook → real best_bid/best_ask → пересчитать realistic edge
  5. Alert если realistic_edge > MIN_EDGE_BPS

Запуск (на VPS):
    python -m scripts.research.negrisk_arb_scanner --once
    python -m scripts.research.negrisk_arb_scanner --interval 5m

Рекомендация: запускать в Docker-контейнере backend, чтобы CLOB API был доступен без 403.
"""

import argparse
import asyncio
import json
import os
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import httpx
import structlog

logger = structlog.get_logger()

GAMMA_EVENTS = "https://gamma-api.polymarket.com/events"
CLOB_BOOK = "https://clob.polymarket.com/book"

# OUT_DIR конфигурируется через env. Дефолт `./research_out/negrisk_arb` —
# работает в любом контейнере без extra mount'ов. На VPS можно указать
# писать в /mnt/data/research/negrisk_arb (требует mount в docker-compose).
OUT_DIR = Path(os.getenv("NEGRISK_OUT_DIR", "./research_out/negrisk_arb"))
MIN_EDGE_BPS = 50  # 0.5% минимальный edge для record (в JSONL)
MIN_LIQ_USD = 1_000  # минимальная ликвидность активного маркета
MIN_VOL24_USD = 500  # минимальный 24h объём события (Phase 1.2: long-tail mispricing)
MIN_LEVEL_SIZE_USD = 50  # adjusted bid/ask: игнорировать dust orders < $50 (Phase 1.3)
ORDERBOOK_TIMEOUT = 8.0
ORDERBOOK_CONCURRENCY = 10
EDGE_ZONE_PCT = 0.03  # ±3% от 1.0 — зона интереса для CLOB-валидации (расширено с 5%)
MAX_CANDIDATES_TO_VALIDATE = 30  # Phase 1.1: больше кандидатов на валидацию

# TG алерт-пороги (более жёсткие чем JSONL record):
# 50bps = шум, 100bps = реальная аномалия достойная внимания.
# vol24 ≥ $20K = есть глубина для realistic execution.
TG_ALERT_MIN_EDGE_BPS = 100
TG_ALERT_MIN_VOL24_USD = 20_000


@dataclass
class MarketLeg:
    question: str
    yes_token: str
    last_trade_yes: float
    liquidity: float
    is_other: bool = False
    best_bid: float | None = None
    best_ask: float | None = None
    # Phase 1.3: adjusted = best price for level with size >= MIN_LEVEL_SIZE_USD
    adj_bid: float | None = None
    adj_ask: float | None = None
    # Phase 1.5: depth для оценки realistic captureable USD
    bid_depth_3lvl_usd: float | None = None
    ask_depth_3lvl_usd: float | None = None
    book_error: str | None = None


@dataclass
class EventScan:
    slug: str
    event_id: str
    vol24: float
    n_active: int
    has_other: bool
    sum_yes_quoted: float
    # Phase 1.4: метаданные для time-of-day и event-lifecycle анализа
    event_type: str = "binary"  # "binary" if n_active==2 else "multi"
    event_age_days: float | None = None
    time_to_resolve_h: float | None = None
    legs: list[MarketLeg] = field(default_factory=list)
    sum_yes_bid: float | None = None  # после CLOB validation (best bid)
    sum_yes_ask: float | None = None
    sum_yes_adj_bid: float | None = None  # adjusted: ignore dust < $50
    sum_yes_adj_ask: float | None = None
    realistic_edge_bps: float | None = None
    note: str = ""


def _parse_json_field(v: Any) -> Any:
    if v is None:
        return None
    if isinstance(v, str):
        try:
            return json.loads(v)
        except json.JSONDecodeError:
            return None
    return v


def _parse_iso_to_utc(s: str | None) -> datetime | None:
    if not s:
        return None
    try:
        # Polymarket: "2026-10-10T00:00:00Z"
        if s.endswith("Z"):
            s = s[:-1] + "+00:00"
        return datetime.fromisoformat(s)
    except (ValueError, TypeError):
        return None


def _classify_event(ev: dict) -> EventScan | None:
    if not ev.get("negRisk"):
        return None
    markets = ev.get("markets") or []
    if len(markets) < 2:
        return None

    scan = EventScan(
        slug=ev.get("slug", ""),
        event_id=str(ev.get("id", "")),
        vol24=float(ev.get("volume24hr") or 0),
        n_active=0,
        has_other=False,
        sum_yes_quoted=0.0,
    )

    for m in markets:
        is_other = bool(m.get("negRiskOther")) or m.get("groupItemTitle") == "Other"
        outs = _parse_json_field(m.get("outcomes"))
        prices = _parse_json_field(m.get("outcomePrices"))
        cti = _parse_json_field(m.get("clobTokenIds"))

        if is_other:
            scan.has_other = True
            continue

        if not (m.get("active") and not m.get("closed")):
            continue
        if not (outs and prices and cti):
            continue
        try:
            yi = outs.index("Yes")
        except ValueError:
            continue

        try:
            yes_p = float(prices[yi])
            yes_token = str(cti[yi])
        except (ValueError, IndexError, TypeError):
            continue

        leg = MarketLeg(
            question=m.get("question", "")[:80],
            yes_token=yes_token,
            last_trade_yes=yes_p,
            liquidity=float(m.get("liquidity") or 0),
        )
        scan.legs.append(leg)
        scan.sum_yes_quoted += yes_p
        scan.n_active += 1

    if scan.n_active < 2:
        return None

    # Phase 1.4: event metadata
    scan.event_type = "binary" if scan.n_active == 2 else "multi"
    now = datetime.now(UTC)
    created = _parse_iso_to_utc(ev.get("createdAt"))
    end = _parse_iso_to_utc(ev.get("endDate"))
    if created:
        scan.event_age_days = (now - created).total_seconds() / 86_400
    if end:
        scan.time_to_resolve_h = (end - now).total_seconds() / 3600

    return scan


async def _fetch_events(limit: int) -> list[dict]:
    async with httpx.AsyncClient(timeout=20.0) as c:
        r = await c.get(
            GAMMA_EVENTS,
            params={
                "closed": "false",
                "limit": limit,
                "order": "volume24hr",
                "ascending": "false",
            },
        )
        r.raise_for_status()
        return r.json()


async def _fetch_book(client: httpx.AsyncClient, token_id: str) -> dict | None:
    """GET /book?token_id — orderbook snapshot, None on failure."""
    try:
        r = await client.get(CLOB_BOOK, params={"token_id": token_id}, timeout=ORDERBOOK_TIMEOUT)
        if r.status_code != 200:
            return None
        return r.json()
    except (httpx.HTTPError, json.JSONDecodeError):
        return None


def _adjusted_price(levels: list[dict], min_size_usd: float, side: str) -> float | None:
    """
    Phase 1.3: первая цена в стакане где size×price >= min_size_usd.

    Игнорирует stale/dust orders на топе книги. Для side=='bid' идём от лучшей
    (высокой) цены вниз; для 'ask' — от лучшей (низкой) цены вверх. Levels
    приходят уже отсортированными от Polymarket CLOB.
    """
    for lvl in levels:
        try:
            price = float(lvl["price"])
            size = float(lvl["size"])
        except (KeyError, ValueError, TypeError):
            continue
        if price * size >= min_size_usd:
            return price
    return None


def _depth_usd(levels: list[dict], n: int = 3) -> float:
    """Сумма price×size по первым n уровням стакана."""
    total = 0.0
    for lvl in levels[:n]:
        try:
            total += float(lvl["price"]) * float(lvl["size"])
        except (KeyError, ValueError, TypeError):
            continue
    return total


async def _validate_event_books(scan: EventScan) -> None:
    """Заполнить best/adj bid/ask и depth по каждому leg через CLOB."""
    sem = asyncio.Semaphore(ORDERBOOK_CONCURRENCY)

    async def _one(client: httpx.AsyncClient, leg: MarketLeg) -> None:
        async with sem:
            book = await _fetch_book(client, leg.yes_token)
        if not book:
            leg.book_error = "fetch_failed"
            return
        bids = book.get("bids") or []
        asks = book.get("asks") or []
        leg.best_bid = float(bids[0]["price"]) if bids else 0.0
        leg.best_ask = float(asks[0]["price"]) if asks else 1.0
        leg.adj_bid = _adjusted_price(bids, MIN_LEVEL_SIZE_USD, "bid")
        leg.adj_ask = _adjusted_price(asks, MIN_LEVEL_SIZE_USD, "ask")
        leg.bid_depth_3lvl_usd = _depth_usd(bids, n=3)
        leg.ask_depth_3lvl_usd = _depth_usd(asks, n=3)

    async with httpx.AsyncClient(timeout=ORDERBOOK_TIMEOUT) as client:
        await asyncio.gather(*[_one(client, leg) for leg in scan.legs])

    bids = [leg.best_bid for leg in scan.legs if leg.best_bid is not None]
    asks = [leg.best_ask for leg in scan.legs if leg.best_ask is not None]
    adj_bids = [leg.adj_bid for leg in scan.legs if leg.adj_bid is not None]
    adj_asks = [leg.adj_ask for leg in scan.legs if leg.adj_ask is not None]

    if len(bids) != scan.n_active or len(asks) != scan.n_active:
        scan.note = f"book_incomplete: {len(bids)}/{scan.n_active} bids, {len(asks)}/{scan.n_active} asks"
        return

    scan.sum_yes_bid = sum(bids)
    scan.sum_yes_ask = sum(asks)
    if len(adj_bids) == scan.n_active:
        scan.sum_yes_adj_bid = sum(adj_bids)
    if len(adj_asks) == scan.n_active:
        scan.sum_yes_adj_ask = sum(adj_asks)

    # Edge считаем по adjusted prices (если есть полные данные), fallback на best.
    # buy-arb: купить все YES @ ask, payout $1 → revenue = 1 - sum_ask
    # sell-arb: продать все YES @ bid → revenue = sum_bid - 1
    sum_ask_for_edge = scan.sum_yes_adj_ask if scan.sum_yes_adj_ask is not None else scan.sum_yes_ask
    sum_bid_for_edge = scan.sum_yes_adj_bid if scan.sum_yes_adj_bid is not None else scan.sum_yes_bid
    used = "adj" if scan.sum_yes_adj_ask is not None and scan.sum_yes_adj_bid is not None else "best"

    buy_edge = (1.0 - sum_ask_for_edge) * 10_000
    sell_edge = (sum_bid_for_edge - 1.0) * 10_000
    if buy_edge >= sell_edge and buy_edge > 0:
        scan.realistic_edge_bps = buy_edge
        scan.note = f"BUY ({used}) sum_ask={sum_ask_for_edge:.4f}"
    elif sell_edge > 0:
        scan.realistic_edge_bps = sell_edge
        scan.note = f"SELL ({used}) sum_bid={sum_bid_for_edge:.4f}"
    else:
        scan.realistic_edge_bps = 0
        scan.note = f"no-arb ({used}): bid={sum_bid_for_edge:.4f} ask={sum_ask_for_edge:.4f}"


async def scan_once(top: int) -> list[EventScan]:
    events = await _fetch_events(top)
    scans = [s for ev in events if (s := _classify_event(ev))]
    logger.info("negrisk_classified", events=len(events), neg_risk=len(scans))

    # Финализируем real-arb-кандидатов: те где has_other=False ИЛИ sum_yes_quoted >> 1
    # (для has_other True нет buy-arb, но sell-arb через NegRiskAdapter всё ещё возможен —
    # через convert: 1 NO в любом + (1 - sum_yes) USDC = YES во всех остальных)
    candidates = [
        s
        for s in scans
        if s.vol24 >= MIN_VOL24_USD
        and abs(s.sum_yes_quoted - 1.0) >= EDGE_ZONE_PCT
        and not s.has_other  # сначала рассматриваем только чистые случаи
    ]
    logger.info("negrisk_candidates", n=len(candidates))

    if candidates:
        candidates.sort(key=lambda s: abs(s.sum_yes_quoted - 1.0), reverse=True)
        await asyncio.gather(*[_validate_event_books(s) for s in candidates[:MAX_CANDIDATES_TO_VALIDATE]])

    return scans


def _write_jsonl(scans: list[EventScan]) -> Path:
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    ts = datetime.now(UTC)
    out = OUT_DIR / f"{ts.strftime('%Y%m%d')}.jsonl"
    with out.open("a") as f:
        for s in scans:
            row = {
                "ts": ts.isoformat(),
                "hour_utc": ts.hour,  # Phase 1.4: для time-of-day анализа
                "slug": s.slug,
                "event_id": s.event_id,
                "vol24": s.vol24,
                "n_active": s.n_active,
                "event_type": s.event_type,
                "event_age_days": s.event_age_days,
                "time_to_resolve_h": s.time_to_resolve_h,
                "has_other": s.has_other,
                "sum_yes_quoted": s.sum_yes_quoted,
                "sum_yes_bid": s.sum_yes_bid,
                "sum_yes_ask": s.sum_yes_ask,
                "sum_yes_adj_bid": s.sum_yes_adj_bid,
                "sum_yes_adj_ask": s.sum_yes_adj_ask,
                "realistic_edge_bps": s.realistic_edge_bps,
                "note": s.note,
                "legs": [
                    {
                        "q": leg.question,
                        "yes_token": leg.yes_token,
                        "last_trade": leg.last_trade_yes,
                        "best_bid": leg.best_bid,
                        "best_ask": leg.best_ask,
                        "adj_bid": leg.adj_bid,
                        "adj_ask": leg.adj_ask,
                        "bid_depth_3lvl_usd": leg.bid_depth_3lvl_usd,
                        "ask_depth_3lvl_usd": leg.ask_depth_3lvl_usd,
                        "liq": leg.liquidity,
                    }
                    for leg in s.legs
                ],
            }
            f.write(json.dumps(row, ensure_ascii=False) + "\n")
    return out


async def _send_tg_alerts(scans: list[EventScan]) -> int:
    """Отправить TG-алерт если есть scan с edge >= TG_ALERT_MIN_EDGE_BPS и vol24 >= порог.

    Использует app.tasks.notifications.send_admin_message — рассылает всем admin_telegram_ids.
    Возвращает количество отправленных алертов. Failure-tolerant: не падает если TG недоступен.
    """
    if os.getenv("NEGRISK_TG_ALERTS", "0") != "1":
        return 0
    alerts = [
        s for s in scans if (s.realistic_edge_bps or 0) >= TG_ALERT_MIN_EDGE_BPS and s.vol24 >= TG_ALERT_MIN_VOL24_USD
    ]
    if not alerts:
        return 0

    try:
        from app.tasks.notifications import send_admin_message
    except ImportError as e:
        logger.warning("negrisk_tg_import_failed", error=str(e))
        return 0

    sent = 0
    for s in alerts[:5]:  # лимит 5 алертов на скан, защита от спама
        msg = (
            f"🎯 NegRisk arb candidate\n"
            f"Event: {s.slug}\n"
            f"Type: {s.event_type} (n={s.n_active})\n"
            f"Edge: {s.realistic_edge_bps:.0f} bps\n"
            f"Vol24h: ${s.vol24:,.0f}\n"
            f"Note: {s.note}\n"
            f"https://polymarket.com/event/{s.slug}"
        )
        try:
            await send_admin_message(msg)
            sent += 1
        except Exception as e:
            logger.warning("negrisk_tg_send_failed", slug=s.slug, error=str(e))
    return sent


async def _send_tg_error(error: str) -> None:
    """TG-алерт о crash'е сканера (только если NEGRISK_TG_ALERTS=1)."""
    if os.getenv("NEGRISK_TG_ALERTS", "0") != "1":
        return
    try:
        from app.tasks.notifications import send_admin_message

        await send_admin_message(f"🚨 NegRisk scanner error\n{error[:500]}")
    except Exception as e:
        logger.warning("negrisk_tg_error_send_failed", error=str(e))


def _print_table(scans: list[EventScan]) -> None:
    scored = [s for s in scans if s.realistic_edge_bps is not None]
    scored.sort(key=lambda s: s.realistic_edge_bps or 0, reverse=True)
    if not scored:
        print("No CLOB-validated candidates.")
        return
    print(f"\n{'slug':50s} {'n':>3s} {'vol24$':>10s} {'edge_bps':>9s}  note")
    print("-" * 110)
    for s in scored[:15]:
        print(f"{s.slug[:50]:50s} {s.n_active:3d} {s.vol24:10.0f} {(s.realistic_edge_bps or 0):9.1f}  {s.note}")


async def loop(top: int, interval_s: int) -> None:
    while True:
        try:
            scans = await scan_once(top)
            out = _write_jsonl(scans)
            _print_table(scans)
            alerts = [s for s in scans if (s.realistic_edge_bps or 0) >= MIN_EDGE_BPS]
            if alerts:
                logger.warning("negrisk_arb_alert", count=len(alerts), file=str(out))
            await _send_tg_alerts(scans)
        except Exception as e:
            logger.error("negrisk_scan_error", error=str(e))
            await _send_tg_error(f"loop: {type(e).__name__}: {e}")
        await asyncio.sleep(interval_s)


def _parse_interval(s: str) -> int:
    if s.endswith("h"):
        return int(s[:-1]) * 3600
    if s.endswith("m"):
        return int(s[:-1]) * 60
    return int(s)


if __name__ == "__main__":
    p = argparse.ArgumentParser()
    # Phase 1.1: расширили scope с 200 до 1000 events (включая long-tail)
    p.add_argument("--top", type=int, default=1000)
    p.add_argument("--interval", type=str, default="1h")
    p.add_argument("--once", action="store_true")
    args = p.parse_args()

    async def _once() -> None:
        try:
            scans = await scan_once(args.top)
            out = _write_jsonl(scans)
            _print_table(scans)
            print(f"\nWritten: {out}")
            await _send_tg_alerts(scans)
        except Exception as e:
            logger.error("negrisk_scan_error", error=str(e))
            await _send_tg_error(f"once: {type(e).__name__}: {e}")
            raise

    if args.once:
        asyncio.run(_once())
    else:
        asyncio.run(loop(args.top, _parse_interval(args.interval)))
