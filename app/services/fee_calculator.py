# app/services/fee_calculator.py
"""Калькулятор комиссий Polymarket.

Формула: fee = amount × feeRate × (p × (1-p))^exponent
(amount в долларах — shares × price сокращается).

Ставки актуальны с 30 марта 2026: https://docs.polymarket.com/trading/fees

Примечания:
- Gas fee: Polymarket субсидирует gas через Relayer (meta-transactions).
  Пользователь реально НЕ платит gas. Gas-поле сохранено как 0 для совместимости.
- Withdrawal fee: официально отсутствует. Winning shares выкупаются по $1.00 USDC.
- Maker rebate: Crypto 20%, все остальные 25% от taker fee.
  Finance 50% — был launch incentive 2026-03-30, возможно снижен до 25%.
"""

from dataclasses import dataclass


@dataclass
class FeeBreakdown:
    trading_fee: float
    gas_fee: float
    opportunity_cost: float
    total: float


# Polymarket taker fee schedule (effective 2026-03-30)
# (feeRate, exponent) — fee = amount × feeRate × (p × (1-p))^exponent
# Источник: https://docs.polymarket.com/trading/fees
_FEE_SCHEDULE: dict[str, tuple[float, float]] = {
    "crypto": (0.072, 1),  # peak 1.80%
    "sports": (0.030, 1),  # peak 0.75%
    "finance": (0.040, 1),  # peak 1.00%, maker rebate 25% (был 50% при запуске)
    "politics": (0.040, 1),  # peak 1.00%
    "economics": (0.050, 1),  # peak 1.25%
    "culture": (0.050, 1),  # peak 1.25%
    "weather": (0.050, 1),  # peak 1.25%
    "tech": (0.040, 1),  # peak 1.00%
    "mentions": (0.040, 1),  # peak 1.00%
    "other": (0.050, 1),  # peak 1.25%
    "geopolitics": (0.0, 1),  # fee-free
}

# Маппинг наших категорий (из markets.category) → Polymarket fee category
_CATEGORY_MAP: dict[str, str] = {
    "tech & ai": "tech",
    "world & economy": "geopolitics",  # fee-free — геополитика/мировые события
}

_DEFAULT_FEE: tuple[float, float] = (0.050, 1)  # fallback: 1.25% peak

# Polymarket maker rebate (% от taker fee возвращается maker'у).
# Источник: https://docs.polymarket.com/trading/fees + reality check via Gamma
# feeSchedule.rebateRate (verified 2026-05-21: crypto=0.20, остальные=0.25).
_MAKER_REBATE: dict[str, float] = {
    "crypto": 0.20,
    "sports": 0.25,
    "finance": 0.25,  # был 0.50 при запуске 2026-03-30, теперь 0.25
    "politics": 0.25,
    "economics": 0.25,
    "culture": 0.25,
    "weather": 0.25,
    "tech": 0.25,
    "mentions": 0.25,
    "other": 0.25,
    "geopolitics": 0.0,  # fee-free → rebate бессмыслен
}

_DEFAULT_MAKER_REBATE = 0.25


def _resolve_category(category: str) -> str:
    """Привести категорию к ключу fee schedule."""
    cat = category.lower().strip()
    return _CATEGORY_MAP.get(cat, cat)


def _resolve_fee_params(
    category: str,
    fee_schedule: dict | None = None,
) -> tuple[float, float]:
    """Вернуть (rate, exponent) либо из per-market feeSchedule, либо из категорийной таблицы.

    Приоритет:
        1. fee_schedule — Polymarket-authoritative значения, sourced from
           Gamma API на каждый market. Используем если есть валидный rate.
        2. _FEE_SCHEDULE[category] — hardcoded fallback (актуальный snapshot
           на 2026-03-30, но устаревает когда Polymarket меняет fees).
    """
    if fee_schedule:
        rate = fee_schedule.get("rate")
        if isinstance(rate, int | float) and rate >= 0:
            exponent = fee_schedule.get("exponent", 1)
            if not isinstance(exponent, int | float) or exponent <= 0:
                exponent = 1
            return (float(rate), float(exponent))
    key = _resolve_category(category)
    return _FEE_SCHEDULE.get(key, _DEFAULT_FEE)


def _resolve_rebate_rate(category: str, fee_schedule: dict | None = None) -> float:
    """Maker rebate (% от taker fee). Приоритет: feeSchedule → категория.

    Returns 0.0..1.0. По умолчанию (unknown category, нет schedule) — 0.25.
    """
    if fee_schedule:
        rebate = fee_schedule.get("rebateRate")
        if isinstance(rebate, int | float) and 0.0 <= rebate <= 1.0:
            return float(rebate)
    return _MAKER_REBATE.get(_resolve_category(category), _DEFAULT_MAKER_REBATE)


def _trading_fee(
    amount: float,
    price: float,
    category: str,
    fee_schedule: dict | None = None,
    is_maker: bool = False,
) -> float:
    """Комиссия trader'а: amount × feeRate × (p × (1-p))^exponent.

    Если передан fee_schedule (per-market от Gamma) — используем его параметры,
    иначе fallback на категорийную таблицу.

    is_maker=True: применяем maker rebate (taker fee × (1 - rebate)).
    """
    rate, k = _resolve_fee_params(category, fee_schedule)
    if rate == 0.0:
        return 0.0
    taker_fee = amount * rate * (price * (1.0 - price)) ** k
    if not is_maker:
        return taker_fee
    rebate = _resolve_rebate_rate(category, fee_schedule)
    return taker_fee * (1.0 - rebate)


def estimate_trading_fee(
    amount: float,
    price: float,
    category: str,
    fee_schedule: dict | None = None,
    is_maker: bool = False,
) -> float:
    """Быстрая оценка trading fee (без gas/opp cost). Для EV gate.

    fee_schedule — опциональный per-market dict из markets.fee_schedule
    (формат Gamma: {rate, exponent, takerOnly, rebateRate}). Если задан и
    валиден — используем его, иначе fallback на категорийную таблицу.

    is_maker — если True, применяем maker rebate (taker_fee × (1 - rebate)).
    По умолчанию считаем worst case = taker.
    """
    return _trading_fee(amount, price, category, fee_schedule, is_maker)


def calculate(
    filled_amount: float,
    price: float,
    category: str,
    profile: str,
    days_to_resolve: float,
    gas_per_trade: float = 0.0,
    annual_rate: float = 0.05,
    fee_schedule: dict | None = None,
    is_maker: bool = False,
) -> FeeBreakdown:
    """Рассчитать все комиссии для одной ставки.

    Gas fee = 0: Polymarket субсидирует все on-chain транзакции через Relayer.
    Opportunity cost включён как альтернативная стоимость заморозки капитала.

    fee_schedule — опциональный per-market dict (см. estimate_trading_fee).
    is_maker — применять maker rebate.
    """
    if profile == "free":
        return FeeBreakdown(0.0, 0.0, 0.0, 0.0)

    trading = round(_trading_fee(filled_amount, price, category, fee_schedule, is_maker), 4)
    gas = gas_per_trade  # 0.0 — gas субсидирован Polymarket
    opp = round(filled_amount * annual_rate * (days_to_resolve / 365.0), 4)

    return FeeBreakdown(
        trading_fee=trading,
        gas_fee=gas,
        opportunity_cost=opp,
        total=round(trading + gas + opp, 4),
    )
