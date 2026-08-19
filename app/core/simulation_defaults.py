"""Backend SSoT для параметров симуляции."""

from app.core.scoring import BayesianScoreCalculator

SIMULATION_DEFAULTS: dict = {
    "stake": 10.0,
    "initial_bank": 10000.0,
    "min_divergence": 0.03,
    "entry_price_floor": 0.15,
    "fee_profile": "direct",
    "max_event_exposure": 0.10,
    "max_category_exposure": 0.30,
    "max_portfolio_heat": 0.40,
    "drawdown_threshold": 0.20,
    "liquidity_threshold": 50000.0,
}

BANK_DEFAULTS: dict = {
    "use_daily_bank": True,
    "daily_limit": 500.0,
    "reinvest_rate": 0.10,
}

LIVE_TRADING_DEFAULTS: dict = {
    "initial_bank": 10000,
    "daily_limit_per_model": 500,
    "reinvest_rate": 0.10,
    "max_bets_per_model": 50,
    "min_divergence": 0.03,
    "max_divergence": 0.30,
    "entry_price_floor": 0.15,
}


def get_min_divergence(category: str | None = None) -> float:
    """Category-aware min_divergence. Fallback на global default."""
    calc = BayesianScoreCalculator()
    return calc.get_min_divergence(category)
