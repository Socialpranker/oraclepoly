"""D49 strike-ladder matcher — чистые regex-функции без I/O.

Провенанс: перенесено из `scripts/research/d49_strike_ladder_backtest.py`
(бэктест-вердикт `research/d49-strike-ladder-2026-07-09/answer.md`, KEEP →
shadow). Три критичных фикса матчера, найденные на бэктесте, сохранены:

1. `end_date` берётся из БД (markets.end_date), НЕ парсится из текста вопроса —
   форматы дат в question разнородны ("on <Date>", "in <Month>", "by <Date>").
2. `window_key` (нормализованный хвост вопроса после strike-фразы) обязателен
   в ключе группировки лесенки — без него один и тот же end_date склеивает
   рынки с разным окном наблюдения ("in August" vs "August 25-31"), давая
   ложные non-monotonic пары.
3. Только строго однонаправленные "above/over/greater than/exceed/surpass $X".
   "reach"/"hit" исключены: touch-anytime барьеры по разные стороны от спота
   не обязаны быть монотонны (verified на SOL "reach $130/$250 in November" —
   разные семантики, не лесенка). "dip to/less than/between/below/or $" тоже
   исключены — не однонаправленные "above $K" вопросы.

Гипотеза: для крипто-лесенки "<ASSET> above $K by <date>" обязано
P(YES > K1) >= P(YES > K2) при K1 < K2. Нарушение = model-free арбитраж.
"""

from __future__ import annotations

import re
from collections import defaultdict
from dataclasses import dataclass
from datetime import date, datetime

_ASSET_ALIASES: dict[str, str] = {
    "bitcoin": "BTC",
    "btc": "BTC",
    "ethereum": "ETH",
    "eth": "ETH",
    "solana": "SOL",
    "sol": "SOL",
    "xrp": "XRP",
    "ripple": "XRP",
    "dogecoin": "DOGE",
    "doge": "DOGE",
}
_ASSET_RE = re.compile(
    r"\b(" + "|".join(sorted(_ASSET_ALIASES, key=len, reverse=True)) + r")\b",
    re.IGNORECASE,
)

# СТРОГО однонаправленные "above/greater than/over/exceed/surpass $X" — валидная
# лесенка P(>K), монотонно убывающая по K. См. docstring модуля §3.
_STRIKE_RE = re.compile(
    r"\b(?:above|over|greater than|exceed|surpass)\s*\$\s*"
    r"(?P<amount>[\d,]+(?:\.\d+)?)\s*(?P<suffix>k|m|b|billion|trillion)?\b",
    re.IGNORECASE,
)

_EXCLUDE_RE = re.compile(
    r"\bdip to\b|\bless than\b|\bbetween\b|\bbelow\b|\bor\s+\$|\breach\b|\bhit\b",
    re.IGNORECASE,
)

# Нормализация "хвоста" вопроса (всё после $strike) в ключ окна резолва.
# См. docstring модуля §2.
_STRIP_STRIKE_RE = re.compile(
    r"\b(?:above|over|greater than|exceed|surpass)\s*\$\s*[\d,]+(?:\.\d+)?\s*(?:k|m|b|billion|trillion)?\b",
    re.IGNORECASE,
)


def _resolution_window_key(question: str) -> str:
    """Хвост вопроса после вырезания strike-фразы, lowercased+trimmed — прокси
    окна резолва. Пары матчатся только при одинаковом ключе (см. docstring)."""
    tail = _STRIP_STRIKE_RE.sub("", question)
    tail = tail.strip().rstrip("?").strip().lower()
    return tail


@dataclass(frozen=True)
class ParsedStrike:
    entity: str
    strike: float
    end_date: date
    market_id: str  # markets.polymarket_id (0x-condition_id), НЕ markets.id UUID
    question: str
    window_key: str
    token_id: str | None = None


def _parse_strike_amount(m: re.Match) -> float:
    amount = float(m.group("amount").replace(",", ""))
    suffix = (m.group("suffix") or "").lower()
    if suffix == "k":
        amount *= 1_000
    elif suffix == "m":
        amount *= 1_000_000
    elif suffix in ("b", "billion"):
        amount *= 1_000_000_000
    elif suffix == "trillion":
        amount *= 1_000_000_000_000
    return amount


def match_strike_ladder_market(
    *, question: str, end_date: date, market_id: str, token_id: str | None = None
) -> ParsedStrike | None:
    """Парсит "Will BTC be above $100,000 on October 3?" -> ParsedStrike.

    None (не best-effort) для range-вопросов ("between $X and $Y"), обратного
    направления ("below"/"less than"/"dip to"), touch-семантики ("reach"/"hit"),
    отсутствия распознанного актива/strike — двусмысленность здесь стоит
    реальных денег (ложная лесенка = legging risk на арбитраже).
    """
    if _EXCLUDE_RE.search(question):
        return None

    asset_match = _ASSET_RE.search(question)
    if asset_match is None:
        return None
    entity = _ASSET_ALIASES[asset_match.group(1).lower()]

    strike_match = _STRIKE_RE.search(question)
    if strike_match is None:
        return None
    strike = _parse_strike_amount(strike_match)
    window_key = _resolution_window_key(question)

    return ParsedStrike(
        entity=entity,
        strike=strike,
        end_date=end_date,
        market_id=market_id,
        question=question,
        window_key=window_key,
        token_id=token_id,
    )


def build_ladders(candidates: list[dict]) -> dict[tuple[str, date, str], list[ParsedStrike]]:
    """Группирует candidate-рынки по (entity, end_date, window_key), лесенка =
    >=2 разных страйков.

    candidates: list of dict с ключами question (str), end_date (date|datetime),
    market_id (str, polymarket_id), token_id (str|None опционально).

    window_key обязателен в ключе группировки — без него лесенка склеивает
    рынки с разным окном наблюдения (см. docstring модуля §2).
    """
    groups: dict[tuple[str, date, str], list[ParsedStrike]] = defaultdict(list)
    for c in candidates:
        end_date_raw = c.get("end_date")
        if end_date_raw is None:
            continue
        end_date = end_date_raw.date() if isinstance(end_date_raw, datetime) else end_date_raw
        parsed = match_strike_ladder_market(
            question=c.get("question") or "",
            end_date=end_date,
            market_id=c["market_id"],
            token_id=c.get("token_id"),
        )
        if parsed is None:
            continue
        groups[(parsed.entity, parsed.end_date, parsed.window_key)].append(parsed)

    ladders: dict[tuple[str, date, str], list[ParsedStrike]] = {}
    for key, items in groups.items():
        # dedupe по strike: если на тот же (entity, end_date, window_key, strike)
        # несколько market_id с РАЗНЫМ вопросом — это разные рынки, случайно
        # схлопнувшиеся в один strike-слот текстовым парсингом — пропускаем
        # весь strike, не гадаем какой market_id "правильный".
        by_strike: dict[float, list[ParsedStrike]] = defaultdict(list)
        for it in items:
            by_strike[it.strike].append(it)
        distinct: list[ParsedStrike] = []
        for _strike, cands in by_strike.items():
            if len({c.market_id for c in cands}) > 1:
                continue
            distinct.append(cands[0])
        distinct.sort(key=lambda p: p.strike)
        if len(distinct) >= 2:
            ladders[key] = distinct
    return ladders
