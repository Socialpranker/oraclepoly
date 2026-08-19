"""Load current prod Oracle config from settings (app/core/config.py) into ScoringConfig."""

from __future__ import annotations

from app.core.config import get_settings
from app.models.feature_vector import ScoringConfig

# Maps scoring_w_* settings attributes → ScoringConfig.weights keys.
# NOTE: w_sports_odds / w_retail intentionally omitted — FeatureVector has no
# sports_odds_raw / retail_factor_raw columns, so those weights can't be scored.
_SETTINGS_TO_WEIGHT_MAP = {
    "scoring_w_mkt": "w_mkt",
    "scoring_w_whale": "w_whale",
    "scoring_w_whale_contra": "w_whale_contra",
    "scoring_w_fresh": "w_fresh",
    "scoring_w_cluster": "w_cluster",
    "scoring_w_registry": "w_registry",
    "scoring_w_oi": "w_oi",
    "scoring_w_concentration": "w_concentration",
    "scoring_w_micro": "w_micro",
    "scoring_w_maker_direction": "w_maker_direction",
    "scoring_w_taker_imbalance": "w_taker_imbalance",
}


def load_prod_config() -> ScoringConfig:
    """Read current prod scoring weights from settings → ScoringConfig.

    All weights in `_SETTINGS_TO_WEIGHT_MAP` are sourced from settings. Defaults
    match `BayesianScoreCalculator.DEFAULT_WEIGHTS` (app/core/scoring.py), so
    behaviour is unchanged unless a `SCORING_W_*` env var overrides them.
    """
    s = get_settings()
    weights: dict[str, float] = {}
    for settings_attr, weight_key in _SETTINGS_TO_WEIGHT_MAP.items():
        val = getattr(s, settings_attr, None)
        if val is not None:
            weights[weight_key] = float(val)

    return ScoringConfig(
        label="prod",
        weights=weights,
        formula_flags={"taker_mode": "contrarian"},
        calibration_source="global",
    )
