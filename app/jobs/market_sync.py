"""
Market Sync Job

Scheduled job to fetch active markets from Polymarket and sync to Oracle.
"""

import gc
import json
import re
from datetime import UTC, datetime
from typing import Any

import structlog

from app.core.categories import CATEGORY_FALLBACK
from app.jobs.negrisk_sync import _extract_metadata as _extract_negrisk_metadata
from app.services.database import fetchrow
from app.services.polymarket_service import get_polymarket_service

logger = structlog.get_logger()


CATEGORY_KEYWORDS = {
    "Politics": [
        "election",
        "president",
        "government",
        "senate",
        "congress",
        "parliament",
        "minister",
        "vote",
        "political",
        "democrat",
        "republican",
        "trump",
        "biden",
        "party",
        "coalition",
        "governor",
        "mayor",
        "legislation",
        "law",
        "regulation",
        "veto",
        "impeach",
        "nomination",
        "primary",
        "candidate",
        "ballot",
        "midterm",
        "prime minister",
        "leader of",
    ],
    "Sports": [
        "win on 20",
        "nba",
        "nfl",
        "mlb",
        "nhl",
        "fifa",
        "uefa",
        "champion",
        "league",
        "match",
        "game",
        "tournament",
        "playoff",
        "super bowl",
        "world cup",
        "olympics",
        "score",
        "team",
        "player",
        "coach",
        "season",
        "atletico",
        "barcelona",
        "madrid",
        "manchester",
        "liverpool",
        "chelsea",
        "arsenal",
        "juventus",
        "lyon",
        "olympique",
        "psg",
        "bayern",
        "finals",
        "stanley cup",
        "world series",
        "premier league",
        "la liga",
        "bundesliga",
        "serie a",
        "ligue 1",
    ],
    "Crypto": [
        "bitcoin",
        "btc",
        "ethereum",
        "eth",
        "crypto",
        "token",
        "blockchain",
        "defi",
        "nft",
        "solana",
        "sol",
        "cardano",
        "dogecoin",
        "doge",
        "binance",
        "coinbase",
        "altcoin",
        "mining",
        "halving",
        "staking",
        "airdrop",
        "memecoin",
        "usdt",
        "usdc",
        "stablecoin",
        "web3",
    ],
    "Tech & AI": [
        "ai",
        "artificial intelligence",
        "openai",
        "chatgpt",
        "gpt",
        "claude",
        "google",
        "apple",
        "microsoft",
        "meta",
        "nvidia",
        "tesla",
        "spacex",
        "elon musk",
        "robot",
        "autonomous",
        "machine learning",
        "semiconductor",
        "chip",
        "tech",
        "software",
        "startup",
        "ipo",
    ],
    "Culture": [
        "movie",
        "film",
        "oscar",
        "grammy",
        "emmy",
        "music",
        "album",
        "celebrity",
        "award",
        "box office",
        "tv show",
        "series",
        "netflix",
        "disney",
        "streaming",
        "entertainment",
        "concert",
        "festival",
        "taylor swift",
        "beyonce",
        "drake",
        "halftime",
        "youtube",
        "views",
        "viral",
        "tiktok",
        "influencer",
    ],
    "World & Economy": [
        "war",
        "ukraine",
        "russia",
        "china",
        "israel",
        "gaza",
        "iran",
        "nato",
        "sanction",
        "gdp",
        "inflation",
        "fed",
        "interest rate",
        "recession",
        "unemployment",
        "oil",
        "commodity",
        "trade",
        "tariff",
        "economy",
        "economic",
        "geopolitical",
        "ceasefire",
        "diplomatic",
        "ambassador",
        "un ",
        "eu ",
    ],
}


def _parse_iso_datetime(value: Any) -> datetime | None:
    """Конвертировать ISO строку в datetime для asyncpg.

    asyncpg не принимает строки для timestamp-колонок — только datetime объекты.
    """
    if value is None:
        return None
    if isinstance(value, datetime):
        return value
    if isinstance(value, str):
        try:
            return datetime.fromisoformat(value.replace("Z", "+00:00"))
        except (ValueError, TypeError):
            return None
    return None


_KEYWORD_REGEX_CACHE: dict[tuple[str, ...], re.Pattern[str]] = {}


def _keyword_pattern(keywords: tuple[str, ...]) -> re.Pattern[str]:
    """Return cached \\b-bounded regex for the keyword tuple.

    Word-boundary matching prevents substring traps such as `sol` matching
    inside `resolve` (Crypto false-positive bug, 2026-05-04).
    """
    pattern = _KEYWORD_REGEX_CACHE.get(keywords)
    if pattern is None:
        joined = "|".join(re.escape(kw) for kw in keywords)
        pattern = re.compile(rf"\b(?:{joined})\b", re.IGNORECASE)
        _KEYWORD_REGEX_CACHE[keywords] = pattern
    return pattern


def infer_category(question: str, description: str = "") -> str:
    """Infer market category from question and description using keyword matching.

    Uses word-boundary regex (`\\b`) so short keywords (`sol`, `ai`, `meta`)
    don't false-match inside longer words (`resolve`, `Russia`, `metabolism`).
    """
    text = f"{question} {description}"

    scores: dict[str, int] = {}
    for category, keywords in CATEGORY_KEYWORDS.items():
        pattern = _keyword_pattern(tuple(keywords))
        matches = pattern.findall(text)
        if matches:
            scores[category] = len(matches)

    if scores:
        return max(scores, key=scores.get)
    return CATEGORY_FALLBACK


def parse_outcome_price(outcome_prices: Any) -> float:
    """Parse outcome prices which can be string JSON or list."""
    if not outcome_prices:
        return 0.5

    # If string, parse as JSON
    if isinstance(outcome_prices, str):
        try:
            outcome_prices = json.loads(outcome_prices)
        except json.JSONDecodeError:
            return 0.5

    # Now should be a list
    if isinstance(outcome_prices, list) and len(outcome_prices) > 0:
        try:
            return float(outcome_prices[0])
        except (ValueError, TypeError):
            return 0.5

    return 0.5


def _coerce_jsonb_list(raw: Any) -> str | None:
    """Gamma может вернуть массив как string JSON либо как list. Нормализуем для jsonb upsert."""
    if raw is None or raw == "":
        return None
    if isinstance(raw, str):
        try:
            parsed = json.loads(raw)
            return json.dumps(parsed) if isinstance(parsed, list) else None
        except (json.JSONDecodeError, ValueError):
            return None
    if isinstance(raw, list):
        return json.dumps(raw)
    return None


def _coerce_numeric(raw: Any) -> float | None:
    """Gamma возвращает numeric поля как string или number. Возвращает None если parse fail."""
    if raw is None or raw == "":
        return None
    try:
        return float(raw)
    except (ValueError, TypeError):
        return None


def _coerce_bool(raw: Any) -> bool | None:
    """Возвращает None если поле отсутствует — чтобы не overwrite валидное значение."""
    if raw is None:
        return None
    return bool(raw)


def _coerce_iso_datetime(raw: Any) -> datetime | None:
    """Wrapper над _parse_iso_datetime с None passthrough."""
    if not raw:
        return None
    return _parse_iso_datetime(raw)


def extract_gamma_extended_fields(market_data: dict[str, Any]) -> dict[str, Any]:
    """Извлечь расширенные Gamma fields для записи в markets table.

    Pure function — никаких I/O. Тестируется на sample response.

    Поля:
        UMA: uma_resolution_status, uma_resolution_statuses (jsonb str), uma_bond, uma_reward
        Term-structure: one_hour/day/week/month/year_price_change
        Cohort flags: cyom, restricted, neg_risk_augmented, automatically_active
        Lifecycle: accepting_orders_timestamp, deploying_timestamp
        Liquidity: competitive (0..1, денормализован из родительского event)
        Fees: fee_schedule (per-market {rate, exponent, takerOnly, rebateRate})
        Safety-net: raw (full response as jsonb str)

    competitive пробрасывается через `market_data["_event_competitive"]`
    (process_event кладёт его перед циклом по markets). Если ключа нет —
    None, UPDATE не затирает существующее значение в БД.

    Returns:
        dict; None значения пишутся как None (UPDATE skip).
    """
    return {
        "uma_resolution_status": market_data.get("umaResolutionStatus") or None,
        "uma_resolution_statuses": _coerce_jsonb_list(market_data.get("umaResolutionStatuses")),
        "uma_bond": _coerce_numeric(market_data.get("umaBond")),
        "uma_reward": _coerce_numeric(market_data.get("umaReward")),
        "one_hour_price_change": _coerce_numeric(market_data.get("oneHourPriceChange")),
        "one_day_price_change": _coerce_numeric(market_data.get("oneDayPriceChange")),
        "one_week_price_change": _coerce_numeric(market_data.get("oneWeekPriceChange")),
        "one_month_price_change": _coerce_numeric(market_data.get("oneMonthPriceChange")),
        "one_year_price_change": _coerce_numeric(market_data.get("oneYearPriceChange")),
        "cyom": _coerce_bool(market_data.get("cyom")),
        "restricted": _coerce_bool(market_data.get("restricted")),
        "neg_risk_augmented": _coerce_bool(market_data.get("negRiskAugmented")),
        "automatically_active": _coerce_bool(market_data.get("automaticallyActive")),
        "accepting_orders_timestamp": _coerce_iso_datetime(market_data.get("acceptingOrdersTimestamp")),
        "deploying_timestamp": _coerce_iso_datetime(market_data.get("deployingTimestamp")),
        "competitive": _coerce_numeric(market_data.get("_event_competitive")),
        "fee_schedule": _coerce_fee_schedule(market_data.get("feeSchedule")),
        "raw": json.dumps(market_data, default=str),
    }


def _coerce_fee_schedule(raw: Any) -> str | None:
    """Нормализовать Gamma feeSchedule для jsonb upsert.

    Принимаем только dict с обязательным `rate`. Остальные поля (exponent,
    takerOnly, rebateRate) опциональны — но если рейт отсутствует, возвращаем
    None (UPDATE не затирает существующее значение через COALESCE).

    Returns:
        JSON string или None.
    """
    if not isinstance(raw, dict):
        return None
    rate = _coerce_numeric(raw.get("rate"))
    if rate is None:
        return None
    cleaned: dict[str, Any] = {"rate": rate}
    exponent = _coerce_numeric(raw.get("exponent"))
    if exponent is not None:
        cleaned["exponent"] = exponent
    if "takerOnly" in raw:
        cleaned["takerOnly"] = bool(raw["takerOnly"])
    rebate = _coerce_numeric(raw.get("rebateRate"))
    if rebate is not None:
        cleaned["rebateRate"] = rebate
    return json.dumps(cleaned)


async def process_market(market_data: dict[str, Any]) -> dict[str, Any]:
    """Process a single market from Polymarket: upsert into Postgres."""

    # Polymarket IDs
    polymarket_id = market_data.get("conditionId") or market_data.get("id")
    if not polymarket_id:
        return {"status": "error", "reason": "missing_id"}

    # Parse current price from outcomePrices
    current_price = parse_outcome_price(market_data.get("outcomePrices"))

    # Calculate horizon flags
    # Fast Track: < 7 days
    # Long Track: > 30 days
    horizon = "mid"
    end_date_dt = _parse_iso_datetime(market_data.get("endDate"))
    if end_date_dt:
        try:
            now = datetime.now(UTC)
            days_left = (end_date_dt - now).days

            if days_left <= 7:
                horizon = "fast"
            elif days_left >= 30:
                horizon = "long"
        except (ValueError, TypeError):
            pass

    # Prepare data for Postgres
    # Note: Gamma API uses camelCase, our DB uses snake_case

    # Extract clobTokenIds for market resolution
    clob_token_ids = market_data.get("clobTokenIds", [])
    if isinstance(clob_token_ids, str):
        try:
            import json as _json

            clob_token_ids = _json.loads(clob_token_ids)
        except (json.JSONDecodeError, ValueError):
            clob_token_ids = []

    # Fix 2.2: outcome name mapping (Yes/No → human-readable name)
    # Gamma API: outcomes = '["Trail Blazers", "Nets"]' (index 0 = YES, 1 = NO)
    token_outcomes = {}
    outcomes_raw = market_data.get("outcomes")
    if outcomes_raw:
        try:
            outcomes_list = json.loads(outcomes_raw) if isinstance(outcomes_raw, str) else outcomes_raw
            if isinstance(outcomes_list, list) and len(outcomes_list) >= 2:
                token_outcomes = {"Yes": outcomes_list[0], "No": outcomes_list[1]}
        except (json.JSONDecodeError, ValueError):
            pass
    # Fallback: tokens array (старый формат, Gamma API больше не возвращает)
    if not token_outcomes:
        tokens = market_data.get("tokens") or []
        for token in tokens:
            token_id = token.get("token_id", "")
            outcome = token.get("outcome", "")
            if token_id and outcome:
                token_outcomes[token_id] = outcome

    question = market_data.get("question", "")
    description = market_data.get("description", "")
    # Category inheritance order:
    # 1. Polymarket-provided market.category (rarely populated, but authoritative when set)
    # 2. Parent event.category (passed via _event_category — from event tags, authoritative)
    # 3. Keyword inference fallback (substring-trap-prone, used only if no event tags match)
    category = (
        market_data.get("category") or market_data.get("_event_category") or infer_category(question, description)
    )
    icon_url = market_data.get("icon")
    volume_24h = float(market_data.get("volume24hr", 0) or 0)
    is_active = not market_data.get("closed", False)
    gamma_resolved = bool(market_data.get("resolved", False))
    gamma_winner = str(market_data.get("winner", "") or "")

    # FIX 2026-05-13: Gamma не помечает resolved/winner поля для closed markets.
    # Истинный outcome содержится в outcomePrices = ["1","0"] (YES won) | ["0","1"] (NO won).
    # Без этого fallback resolved=false → блок всей resolve pipeline.
    if not gamma_resolved and market_data.get("closed", False):
        outcome_prices_raw = market_data.get("outcomePrices")
        if outcome_prices_raw:
            try:
                op = json.loads(outcome_prices_raw) if isinstance(outcome_prices_raw, str) else outcome_prices_raw
                if isinstance(op, list) and len(op) >= 2:
                    yes_p = float(op[0])
                    no_p = float(op[1])
                    # Polymarket терминал: ['1','0'] (YES won) или ['0','1'] (NO won)
                    if yes_p >= 0.95 and no_p <= 0.05:
                        gamma_resolved = True
                        gamma_winner = "yes"
                    elif no_p >= 0.95 and yes_p <= 0.05:
                        gamma_resolved = True
                        gamma_winner = "no"
            except (json.JSONDecodeError, ValueError, TypeError):
                pass
    slug = market_data.get("slug")
    updated_at = datetime.now(UTC)
    event_id = market_data.get("_event_id")

    # 1. Upsert into Postgres
    try:
        if event_id:
            row = await fetchrow(
                """
                INSERT INTO markets (
                    polymarket_id, condition_id, question, description, category,
                    icon_url, current_price, volume_24h, end_date, is_active,
                    horizon, slug, clob_token_ids, token_outcomes, updated_at, event_id
                ) VALUES (
                    $1, $2, $3, $4, $5,
                    $6, $7, $8, $9, $10,
                    $11, $12, $13, $14, $15, $16
                )
                ON CONFLICT (polymarket_id) DO UPDATE SET
                    condition_id = EXCLUDED.condition_id,
                    question = EXCLUDED.question,
                    description = EXCLUDED.description,
                    category = EXCLUDED.category,
                    icon_url = EXCLUDED.icon_url,
                    current_price = EXCLUDED.current_price,
                    volume_24h = EXCLUDED.volume_24h,
                    end_date = EXCLUDED.end_date,
                    is_active = EXCLUDED.is_active,
                    horizon = EXCLUDED.horizon,
                    slug = EXCLUDED.slug,
                    clob_token_ids = EXCLUDED.clob_token_ids,
                    token_outcomes = EXCLUDED.token_outcomes,
                    updated_at = EXCLUDED.updated_at,
                    event_id = EXCLUDED.event_id
                RETURNING id
                """,
                str(polymarket_id),
                str(polymarket_id),
                question,
                description,
                category,
                icon_url,
                current_price,
                volume_24h,
                end_date_dt,
                is_active,
                horizon,
                slug,
                json.dumps(clob_token_ids),
                json.dumps(token_outcomes),
                updated_at,
                event_id,
            )
        else:
            row = await fetchrow(
                """
                INSERT INTO markets (
                    polymarket_id, condition_id, question, description, category,
                    icon_url, current_price, volume_24h, end_date, is_active,
                    horizon, slug, clob_token_ids, token_outcomes, updated_at
                ) VALUES (
                    $1, $2, $3, $4, $5,
                    $6, $7, $8, $9, $10,
                    $11, $12, $13, $14, $15
                )
                ON CONFLICT (polymarket_id) DO UPDATE SET
                    condition_id = EXCLUDED.condition_id,
                    question = EXCLUDED.question,
                    description = EXCLUDED.description,
                    category = EXCLUDED.category,
                    icon_url = EXCLUDED.icon_url,
                    current_price = EXCLUDED.current_price,
                    volume_24h = EXCLUDED.volume_24h,
                    end_date = EXCLUDED.end_date,
                    is_active = EXCLUDED.is_active,
                    horizon = EXCLUDED.horizon,
                    slug = EXCLUDED.slug,
                    clob_token_ids = EXCLUDED.clob_token_ids,
                    token_outcomes = EXCLUDED.token_outcomes,
                    updated_at = EXCLUDED.updated_at
                RETURNING id
                """,
                str(polymarket_id),
                str(polymarket_id),
                question,
                description,
                category,
                icon_url,
                current_price,
                volume_24h,
                end_date_dt,
                is_active,
                horizon,
                slug,
                json.dumps(clob_token_ids),
                json.dumps(token_outcomes),
                updated_at,
            )

        if not row:
            return {"status": "error", "reason": "upsert_failed"}

        internal_id = str(row["id"])
    except Exception as e:
        logger.error("market_upsert_failed", polymarket_id=str(polymarket_id), error=str(e))
        return {"status": "error", "reason": str(e)}

    # 1a. Extended Gamma fields (UMA + term-structure + cohort + raw).
    # См. migrations/20260521083820_extend_markets_gamma_fields.sql.
    extended = extract_gamma_extended_fields(market_data)
    try:
        await fetchrow(
            """
            UPDATE markets SET
                uma_resolution_status      = $2,
                uma_resolution_statuses    = $3::jsonb,
                uma_bond                   = $4,
                uma_reward                 = $5,
                one_hour_price_change      = $6,
                one_day_price_change       = $7,
                one_week_price_change      = $8,
                one_month_price_change     = $9,
                one_year_price_change      = $10,
                cyom                       = $11,
                restricted                 = $12,
                neg_risk_augmented         = $13,
                automatically_active       = $14,
                accepting_orders_timestamp = $15,
                deploying_timestamp        = $16,
                competitive                = COALESCE($17, competitive),
                fee_schedule               = COALESCE($18::jsonb, fee_schedule),
                raw                        = CASE WHEN $20 THEN $19::jsonb ELSE NULL END
            WHERE id = $1
            RETURNING id
            """,
            row["id"],
            extended["uma_resolution_status"],
            extended["uma_resolution_statuses"],
            extended["uma_bond"],
            extended["uma_reward"],
            extended["one_hour_price_change"],
            extended["one_day_price_change"],
            extended["one_week_price_change"],
            extended["one_month_price_change"],
            extended["one_year_price_change"],
            extended["cyom"],
            extended["restricted"],
            extended["neg_risk_augmented"],
            extended["automatically_active"],
            extended["accepting_orders_timestamp"],
            extended["deploying_timestamp"],
            extended["competitive"],
            extended["fee_schedule"],
            extended["raw"],
            # raw — write-only safety-net (0 читателей). У закрытого рынка бесполезен,
            # но sync тянет до 1500 closed events/15мин и без гейта переписывал бы raw
            # обратно после daily cleanup → вечная утечка TOAST (DB Size Alert по markets).
            is_active,
        )
    except Exception as e:
        # Non-fatal: extended fields update провалился, но base upsert уже состоялся.
        # Лучше потерять расширенные поля чем sync всего рынка.
        logger.warning("market_extended_update_failed", polymarket_id=str(polymarket_id), error=str(e))

    # 1b. Gamma resolved/winner → UPDATE + триггер resolve pipeline
    if gamma_resolved and not is_active:
        try:
            await fetchrow(
                "UPDATE markets SET resolved = true, winner = $1 WHERE id = $2 RETURNING id",
                gamma_winner,
                row["id"],
            )
            outcome = None
            if gamma_winner:
                outcome = "yes" if gamma_winner.lower() in ("yes", "1", "true") else "no"
            elif current_price > 0.95:
                outcome = "yes"
            elif current_price < 0.05:
                outcome = "no"

            if outcome:
                from app.tasks.resolve_outcomes import resolve_single_market

                snap_count = await resolve_single_market(internal_id, outcome, current_price)
                if snap_count > 0:
                    logger.info(
                        "market_sync_triggered_resolve",
                        market_id=internal_id,
                        outcome=outcome,
                        snaps=snap_count,
                    )
        except Exception as e:
            logger.warning("market_sync_resolve_trigger_failed", market_id=internal_id, error=str(e))

    # Weather arbitrage reactive analysis удалён в Phoenix V2 cleanup
    # (legacy weather_arbitrage_scan job вырезан, edge не подтверждён).
    return {"status": "synced", "id": internal_id, "polymarket_id": polymarket_id}


_TAG_ALIASES: dict[str, str] = {
    # Politics — elections, parties, political figures
    "Politics": "Politics",
    "Elections": "Politics",
    "Global Elections": "Politics",
    "US Politics": "Politics",
    "US Election": "Politics",
    "World Elections": "Politics",
    "Main Election": "Politics",
    "Primaries": "Politics",
    "Trump": "Politics",
    "Biden": "Politics",
    "Jerome Powell": "Politics",  # Fed chair, more political than economic
    "Economic Policy": "Politics",
    # Sports — all sport categories
    "Sports": "Sports",
    "Soccer": "Sports",
    "Football": "Sports",
    "NBA": "Sports",
    "NFL": "Sports",
    "MLB": "Sports",
    "NHL": "Sports",
    "Basketball": "Sports",
    "baseball": "Sports",
    "Tennis": "Sports",
    "Golf": "Sports",
    "MMA": "Sports",
    "UFC": "Sports",
    "Boxing": "Sports",
    "Cricket": "Sports",
    "Hockey": "Sports",
    "Esports": "Sports",
    "Games": "Sports",
    "league of legends": "Sports",
    "counter strike 2": "Sports",
    "EPL": "Sports",
    "Premier League": "Sports",
    "La Liga": "Sports",
    "Bundesliga": "Sports",
    "F1": "Sports",
    "Formula 1": "Sports",
    "MotoGP": "Sports",
    "Olympics": "Sports",
    # Crypto — coins, tokens, blockchain
    "Crypto": "Crypto",
    "Crypto Prices": "Crypto",
    "Bitcoin": "Crypto",
    "Ethereum": "Crypto",
    "Solana": "Crypto",
    "BTC": "Crypto",
    "ETH": "Crypto",
    "DeFi": "Crypto",
    "NFT": "Crypto",
    "Memecoins": "Crypto",
    # Tech & AI
    "Tech": "Tech & AI",
    "Tech & AI": "Tech & AI",
    "Big Tech": "Tech & AI",
    "AI": "Tech & AI",
    "OpenAI": "Tech & AI",
    "Apple": "Tech & AI",
    "Google": "Tech & AI",
    "Meta": "Tech & AI",
    "Tesla": "Tech & AI",
    "Nvidia": "Tech & AI",
    "Microsoft": "Tech & AI",
    # World & Economy — geopolitics, weather, markets, commodities
    "World": "World & Economy",
    "Geopolitics": "World & Economy",
    "Middle East": "World & Economy",
    "Iran": "World & Economy",
    "Israel": "World & Economy",
    "Ukraine": "World & Economy",
    "Russia": "World & Economy",
    "China": "World & Economy",
    "United Kingdom": "World & Economy",
    "UK": "World & Economy",
    "Iran Ceasefire": "World & Economy",
    "Iran Regime": "World & Economy",
    "U.S. x Iran": "World & Economy",
    "Strait of Hormuz": "World & Economy",
    "Weather": "World & Economy",
    "Highest temperature": "World & Economy",
    "Lowest temperature": "World & Economy",
    "Daily Temperature": "World & Economy",
    "Hurricane": "World & Economy",
    "Earthquake": "World & Economy",
    "Economy": "World & Economy",
    "Finance": "World & Economy",
    "Finance Updown": "World & Economy",
    "Pyth Finance": "World & Economy",
    "Equities": "World & Economy",
    "Stocks": "World & Economy",
    "Commodities": "World & Economy",
    "Oil": "World & Economy",
    "Fed": "World & Economy",
    "Fed Rates": "World & Economy",
    "Business": "World & Economy",
    "Up or Down": "World & Economy",
    # Culture — entertainment, celebrities, media
    "Culture": "Culture",
    "Pop Culture": "Culture",
    "Music": "Culture",
    "Entertainment": "Culture",
    "Movies": "Culture",
    "Reality TV": "Culture",
    "TV": "Culture",
    "Celebrities": "Culture",
    "Awards": "Culture",
    "Oscar": "Culture",
    "Grammy": "Culture",
    "Box Office": "Culture",
    "Streaming": "Culture",
}


def infer_category_from_tags(tags: list[dict], question: str = "", description: str = "") -> str:
    """Infer category from event tags first, fallback to keyword matching.

    Tags that don't map to a known category (operational labels like
    `Recurring`, `Hide From New`, geographic `Mexico`) are skipped — we
    look for a real category tag and fall back to keyword inference.
    """
    if tags:
        for tag in tags:
            label = (tag.get("label") or "").strip()
            mapped = _TAG_ALIASES.get(label)
            if mapped is not None:
                return mapped
    return infer_category(question, description)


async def process_event(event_data: dict[str, Any]) -> dict[str, Any]:
    """Process a single event: upsert event + all nested markets."""
    from app.repositories.event_repository import EventRepository

    repo = EventRepository()

    polymarket_id = str(event_data.get("id", ""))
    if not polymarket_id:
        return {"status": "error", "reason": "missing_event_id"}

    markets_list = event_data.get("markets", [])

    # neg-risk поля (neg_risk/neg_risk_other/n_yes_winners) нужны D3 CrossMktArb.
    # Парсинг делегирован negrisk_sync — один источник правды, weekly-репарация не разъедется.
    negrisk_metadata = _extract_negrisk_metadata(event_data) or {}

    event_record = {
        "polymarket_id": polymarket_id,
        "title": event_data.get("title", ""),
        "slug": event_data.get("slug"),
        "description": event_data.get("description", ""),
        "category": infer_category_from_tags(
            event_data.get("tags", []),
            event_data.get("title", ""),
            event_data.get("description", ""),
        ),
        "image_url": event_data.get("image"),
        "volume": float(event_data.get("volume", 0) or 0),
        "volume_24h": float(event_data.get("volume24hr", 0) or 0),
        "liquidity": float(event_data.get("liquidity", 0) or 0),
        "start_date": _parse_iso_datetime(event_data.get("startDate")),
        "end_date": _parse_iso_datetime(event_data.get("endDate")),
        "is_active": event_data.get("active", True) and not event_data.get("closed", False),
        "markets_count": len(markets_list),
        "tags": json.dumps(event_data.get("tags", [])),
        "competitive": float(event_data.get("competitive", 0) or 0),
        "neg_risk": negrisk_metadata.get("neg_risk", False),
        "neg_risk_other": negrisk_metadata.get("neg_risk_other", False),
        "n_yes_winners": negrisk_metadata.get("n_yes_winners", 1),
        "updated_at": datetime.now(UTC),
    }

    try:
        event_row = await repo.upsert_event(event_record)
        event_internal_id = event_row.get("id")
    except Exception as e:
        logger.error("event_upsert_failed", polymarket_id=polymarket_id, error=str(e))
        return {"status": "error", "reason": str(e)}

    # Process nested markets (DB only, embedding отложен для батча)
    # Markets inherit category from parent event — Polymarket-provided event tags
    # are authoritative, while market-level keyword inference can misfire
    # (e.g. weather markets matched 'sol' inside 'resolve' before PR #431).
    event_category = event_record["category"]
    event_competitive = event_record.get("competitive")
    market_results = []
    for market_data in markets_list:
        market_data["_event_id"] = event_internal_id
        market_data["_event_category"] = event_category
        market_data["_event_competitive"] = event_competitive
        try:
            result = await process_market(market_data)
            market_results.append(result)
        except Exception as e:
            logger.error("market_in_event_failed", error=str(e))
            market_results.append({"status": "error", "error": str(e)})

    # Embeddings disabled — Cloudflare AI removed

    synced_markets = len([r for r in market_results if r.get("status") == "synced"])
    return {
        "status": "synced",
        "event_id": event_internal_id,
        "polymarket_id": polymarket_id,
        "markets_synced": synced_markets,
        "markets_total": len(markets_list),
    }


async def run_market_sync() -> dict[str, Any]:
    """
    Main entry point for the market sync job.
    Fetches events from Polymarket Gamma API (with pagination),
    upserts events + nested markets.
    Called by cron_runner.
    """
    poly_service = get_polymarket_service()

    # Fetch events with pagination (10 pages of 100 = до 1000 событий)
    all_events = []
    for offset in range(0, 1000, 100):
        page = await poly_service.fetch_active_events(limit=100, offset=offset)
        if not page:
            break
        all_events.extend(page)

    if not all_events:
        logger.warning("market_sync_empty_fetch", msg="Gamma Events API returned 0 events")
        return {"events_synced": 0, "markets_synced": 0, "errors": 0, "total_events": 0, "fetch_failed": True}

    logger.info("market_sync_fetched_events", count=len(all_events))

    # Process events чанками по EVENT_CHUNK_SIZE с gc.collect() между ними.
    # Без чанков 1000 events × ~18 markets = ~18000 embeddings → OOM на VPS.
    EVENT_CHUNK_SIZE = 30
    results = []
    seen_event_ids = []

    for chunk_start in range(0, len(all_events), EVENT_CHUNK_SIZE):
        chunk = all_events[chunk_start : chunk_start + EVENT_CHUNK_SIZE]
        logger.info(
            "market_sync_processing_chunk",
            chunk=chunk_start // EVENT_CHUNK_SIZE + 1,
            total_chunks=(len(all_events) + EVENT_CHUNK_SIZE - 1) // EVENT_CHUNK_SIZE,
            events_in_chunk=len(chunk),
        )

        for event in chunk:
            try:
                result = await process_event(event)
                results.append(result)
                if result.get("polymarket_id"):
                    seen_event_ids.append(result["polymarket_id"])
            except Exception as e:
                logger.error("event_process_error", error=str(e))
                results.append({"status": "error", "error": str(e)})

        # Освободить память между чанками
        gc.collect()

    # Sync closed events — обновляет current_price для закрытых рынков (0 или 1),
    # чтобы resolve_outcomes мог определить исход.
    # Без этого шага закрытые рынки остаются с промежуточными ценами (0.05-0.95).
    # 2026-05-13: pagination до 1500 closed events (было 300). У нас 455+ unresolved
    # markets с прошедшим end_date — нужно покрыть.
    closed_synced = 0
    try:
        for offset in range(0, 1500, 300):
            closed_events = await poly_service.fetch_closed_events(limit=300, offset=offset)
            if not closed_events:
                break
            logger.info("market_sync_fetched_closed_events", count=len(closed_events), offset=offset)
            for event in closed_events:
                try:
                    result = await process_event(event)
                    if result.get("status") == "synced":
                        closed_synced += result.get("markets_synced", 0)
                except Exception as e:
                    logger.error("closed_event_process_error", error=str(e))
    except Exception as e:
        logger.warning("closed_events_sync_failed", error=str(e))

    # Deactivate stale events
    from app.repositories.event_repository import EventRepository

    try:
        repo = EventRepository()
        deactivated = await repo.deactivate_stale_events(seen_event_ids)
        logger.info("events_deactivated", count=deactivated)
    except Exception as e:
        logger.warning("deactivate_stale_failed", error=str(e))

    # Qdrant vector cleanup disabled — embeddings removed

    events_synced = len([r for r in results if r.get("status") == "synced"])
    markets_synced = sum(r.get("markets_synced", 0) for r in results if r.get("status") == "synced")
    errors = len([r for r in results if r.get("status") == "error"])

    # Event arbitrage scanner removed в Phoenix V2 (D3 detector использует neg_risk events).
    logger.info("market_sync_complete", events_synced=events_synced, markets_synced=markets_synced, errors=errors)

    return {
        "events_synced": events_synced,
        "markets_synced": markets_synced,
        "closed_markets_synced": closed_synced,
        "errors": errors,
        "total_events": len(all_events),
        "qdrant_cleaned": 0,
        "fetch_failed": False,
    }
