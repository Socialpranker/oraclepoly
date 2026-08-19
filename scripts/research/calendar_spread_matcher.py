"""Regex-матчер календарных серий — D46 calendar-spread arb (research/fable-strategy-brief-2026-07-03/).

Gamma API не отдаёт готового поля группировки рынков-серий с одним активом/сущностью
и разными датами экспирации (verified 2026-07-03 — нет `series`, `groupItemTitle`
используется только для neg-risk, не для этого). Матчинг делается regex-парсингом
question/slug в структурированный кортеж, который потом группируется по
(asset, strike) или (entity,) вызывающим кодом сборщика.

Ложноположительная пара стоит денег на реальном арбитраже (см. brief §про legging
risk) — оба матчера по умолчанию НЕ угадывают: multi-entity/range-вопросы дают None,
а не best-effort парс.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date

_MONTHS = {
    "january": 1,
    "february": 2,
    "march": 3,
    "april": 4,
    "may": 5,
    "june": 6,
    "july": 7,
    "august": 8,
    "september": 9,
    "october": 10,
    "november": 11,
    "december": 12,
    "jan": 1,
    "feb": 2,
    "mar": 3,
    "apr": 4,
    "jun": 6,
    "jul": 7,
    "aug": 8,
    "sep": 9,
    "sept": 9,
    "oct": 10,
    "nov": 11,
    "dec": 12,
}

_ASSET_ALIASES: dict[str, str] = {
    "bitcoin": "BTC",
    "btc": "BTC",
    "ethereum": "ETH",
    "eth": "ETH",
}

_MONTH_NAME_RE = "|".join(_MONTHS)

# "by December 31, 2026" / "by Dec 31 2026" / "by July 31, 2026"
_DATE_RE = re.compile(
    rf"\bby\s+(?P<month>{_MONTH_NAME_RE})\s+(?P<day>\d{{1,2}}),?\s+(?P<year>\d{{4}})\b",
    re.IGNORECASE,
)

# "reach $150,000" / "hit $100k" / "reach $10,000"
_STRIKE_RE = re.compile(
    r"\$\s*(?P<amount>[\d,]+(?:\.\d+)?)\s*(?P<suffix>k)?\b",
    re.IGNORECASE,
)

_RANGE_MARKERS = re.compile(r"\bbetween\b|\brange\b", re.IGNORECASE)

_MULTI_ENTITY_MARKERS = re.compile(r"\bor\b", re.IGNORECASE)


def _parse_date(match: re.Match) -> date | None:
    month = _MONTHS.get(match.group("month").lower())
    if month is None:
        return None
    try:
        return date(int(match.group("year")), month, int(match.group("day")))
    except ValueError:
        return None


def _parse_strike(text: str) -> float | None:
    m = _STRIKE_RE.search(text)
    if not m:
        return None
    amount = float(m.group("amount").replace(",", ""))
    if m.group("suffix"):
        amount *= 1_000
    return amount


def _find_asset(text: str) -> str | None:
    lowered = text.lower()
    for alias, canonical in _ASSET_ALIASES.items():
        if re.search(rf"\b{alias}\b", lowered):
            return canonical
    return None


@dataclass(frozen=True)
class ParsedMarket:
    asset: str
    strike: float
    end_date: date


@dataclass(frozen=True)
class ParsedPoliticsMarket:
    entity: str
    end_date: date


def match_crypto_threshold(*, question: str, slug: str) -> ParsedMarket | None:
    """Парсит crypto threshold-вопросы вида "Will BTC hit $X by DATE?".

    Возвращает None (не best-effort) если вопрос — range ("between $X and $Y"),
    не содержит даты, или актив не распознан — эти случаи не наша серия, гадать дороже,
    чем пропустить.
    """
    if _RANGE_MARKERS.search(question):
        return None

    asset = _find_asset(question)
    if asset is None:
        return None

    date_match = _DATE_RE.search(question)
    if date_match is None:
        return None
    end_date = _parse_date(date_match)
    if end_date is None:
        return None

    strike = _parse_strike(question)
    if strike is None:
        return None

    return ParsedMarket(asset=asset, strike=strike, end_date=end_date)


# "Will <Name Name> be/win/become ... by DATE" — единственная именованная сущность.
_POLITICS_ENTITY_RE = re.compile(
    r"^[Ww]ill\s+(?P<entity>[A-Z][a-z]+(?:\s+[A-Z][a-z]+)+)\s+be\b",
)


def match_politics_series(*, question: str, slug: str) -> ParsedPoliticsMarket | None:
    """Парсит политические серийные вопросы вида "Will <Entity> be ... by DATE?".

    Явно отказывается угадывать при нескольких именованных сущностях в одном
    вопросе ("Jane Doe or John Smith") — двусмысленность здесь дороже пропуска.
    """
    if _MULTI_ENTITY_MARKERS.search(question):
        return None

    entity_match = _POLITICS_ENTITY_RE.search(question.strip())
    if entity_match is None:
        return None

    date_match = _DATE_RE.search(question)
    if date_match is None:
        return None
    end_date = _parse_date(date_match)
    if end_date is None:
        return None

    return ParsedPoliticsMarket(entity=entity_match.group("entity"), end_date=end_date)
