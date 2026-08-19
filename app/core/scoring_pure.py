"""Pure Oracle scoring — two stages, stateless, None-safe, deterministic.

Заменяет BayesianScoreCalculator.calculate_oracle_score() в Phase 2.

API:
  score_raw(fv, cfg)       — Bayesian posterior (raw sigmoid, research).
  apply_calibration(r, mp, cat) — deployment calibration (damping + clamp + eps).
  score(fv, cfg)           — full pipeline, equivalent to legacy output.

Formula (score_raw, §3.5 MATH_SPEC):
    Z_posterior = w_mkt * safe_logit(market_price)
                + w_whale * whale_contribution_raw
                + w_sm_registry * sm_registry_raw
                + w_oi * oi_factor_raw
                + w_maker_direction * maker_direction_raw
                + w_taker_imbalance * taker_imbalance_raw
    P_raw = sigma(Z_posterior / temperature)

Calibration (apply_calibration):
    divergence = P_raw - market_price
    if divergence > 0: divergence *= upward_damping(category)
    divergence = clamp(divergence, ±max_divergence(category))
    P_oracle = clip(market_price + divergence, [ε, 1-ε])
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field

from app.models.feature_vector import FeatureVector, ScoringConfig


@dataclass
class ScoreResult:
    oracle_score: float
    z_posterior: float
    divergence: float
    config_hash: str
    components_used: set[str] = field(default_factory=set)
    breakdown: dict[str, float] = field(default_factory=dict)


_SIG_CLIP = 500.0  # prevent overflow in math.exp


def _safe_logit(p: float) -> float:
    p = max(1e-6, min(1 - 1e-6, p))
    return math.log(p / (1 - p))


def _sigmoid(z: float) -> float:
    z = max(-_SIG_CLIP, min(_SIG_CLIP, z))
    return 1.0 / (1.0 + math.exp(-z))


# Mapping: feature column → config weight key → display name (for breakdown)
_COMPONENT_MAP = [
    ("whale", "whale_contribution_raw", "w_whale"),
    ("sm_fresh", "sm_fresh_raw", "w_fresh"),
    ("sm_cluster", "sm_cluster_raw", "w_cluster"),
    ("sm_registry", "sm_registry_raw", "w_registry"),
    ("oi", "oi_factor_raw", "w_oi"),
    ("conc", "concentration_factor_raw", "w_concentration"),
    ("micro", "micro_factor_raw", "w_micro"),
    ("maker", "maker_direction_raw", "w_maker_direction"),
    ("taker", "taker_imbalance_raw", "w_taker_imbalance"),
]


# Deployment calibration constants (mirror app/core/scoring.py).
# Applied in apply_calibration() as separate stage from score_raw().
_EPSILON = 1e-5
_DEFAULT_UPWARD_DAMPING = 0.30
_DEFAULT_MAX_DIVERGENCE = 0.10

_CATEGORY_UPWARD_DAMPING: dict[str, float] = {
    "weather": 0.15,
    "culture": 0.20,
    "sports": 0.25,
    "politics": 0.25,
    "geopolitics": 0.30,
    "tech & ai": 0.30,
    "crypto": 0.35,
    "finance": 0.35,
}

_CATEGORY_MAX_DIVERGENCE: dict[str, float] = {
    "crypto": 0.12,
    "finance": 0.12,
    "politics": 0.08,
    "sports": 0.08,
    "weather": 0.06,
}


def score_raw(
    fv: FeatureVector,
    cfg: ScoringConfig,
    temperature: float = 1.3,
) -> ScoreResult:
    """Pure Bayesian posterior WITHOUT deployment calibration.

    Returns the raw sigmoid output. For live/prod parity with legacy oracle,
    call apply_calibration() on the result.
    """
    market_price = fv.market_price
    z_mkt = _safe_logit(market_price)
    w_mkt = cfg.weights.get("w_mkt", 1.0)

    z_posterior = w_mkt * z_mkt
    breakdown: dict[str, float] = {"market": w_mkt * z_mkt}
    used: set[str] = set()

    for component_name, feature_attr, weight_key in _COMPONENT_MAP:
        raw = getattr(fv, feature_attr, None)
        weight = cfg.weights.get(weight_key)
        if raw is None or weight is None or weight == 0.0:
            continue
        contribution = weight * float(raw)
        z_posterior += contribution
        breakdown[component_name] = contribution
        used.add(component_name)

    t = temperature if temperature > 0 else 1.0
    oracle_score = _sigmoid(z_posterior / t)
    divergence = oracle_score - market_price

    return ScoreResult(
        oracle_score=oracle_score,
        z_posterior=z_posterior,
        divergence=divergence,
        config_hash=cfg.hash,
        components_used=used,
        breakdown=breakdown,
    )


def apply_calibration(raw: ScoreResult, market_price: float, category: str | None) -> ScoreResult:
    """Apply deployment calibration: upward damping + per-category max divergence clamp + epsilon clip.

    Mirrors BayesianScoreCalculator.calculate_oracle_score post-processing
    (app/core/scoring.py:1047-1056).
    """
    cat_key = category.lower() if category else None
    up_damping = _CATEGORY_UPWARD_DAMPING.get(cat_key, _DEFAULT_UPWARD_DAMPING) if cat_key else _DEFAULT_UPWARD_DAMPING
    max_div = _CATEGORY_MAX_DIVERGENCE.get(cat_key, _DEFAULT_MAX_DIVERGENCE) if cat_key else _DEFAULT_MAX_DIVERGENCE

    divergence = raw.oracle_score - market_price
    if divergence > 0:
        divergence *= up_damping
    clamped_div = max(-max_div, min(max_div, divergence))
    calibrated = max(_EPSILON, min(1 - _EPSILON, market_price + clamped_div))

    return ScoreResult(
        oracle_score=calibrated,
        z_posterior=raw.z_posterior,
        divergence=calibrated - market_price,
        config_hash=raw.config_hash,
        components_used=raw.components_used,
        breakdown=raw.breakdown,
    )


def score(
    fv: FeatureVector,
    cfg: ScoringConfig,
    temperature: float = 1.3,
) -> ScoreResult:
    """Full pipeline: score_raw() + apply_calibration(). Matches legacy BayesianScoreCalculator output.

    Use score_raw() if you want the uncalibrated Bayesian posterior (research).
    """
    raw = score_raw(fv, cfg, temperature=temperature)
    return apply_calibration(raw, fv.market_price, fv.category)
