"""Equivalence tests: score() must produce same oracle_score as BayesianScoreCalculator
on realistic inputs. If this fails, either:
  (a) scoring_pure has a math bug
  (b) BayesianScoreCalculator has extra side-effects (category shrink, ttr, etc.)
     that we need to port OR explicitly exclude from pure function.
"""

from datetime import UTC, datetime

import pytest

from app.core.scoring import BayesianScoreCalculator
from app.core.scoring_pure import score
from app.models.feature_vector import FeatureVector, ScoringConfig

_TOLERANCE = 1e-4  # oracle_score precision


def _make_fv(market_price=0.5, **comps):
    return FeatureVector(
        market_id="m",
        snapshot_at=datetime(2026, 4, 20, tzinfo=UTC),
        side="YES",
        market_price=market_price,
        source="live",
        **comps,
    )


def _legacy_score(fv: FeatureVector, weights: dict) -> float:
    """Invoke BayesianScoreCalculator with the same inputs."""
    calc = BayesianScoreCalculator(weights=weights)
    result = calc.calculate_oracle_score(
        market_price=fv.market_price,
        whale_factors=[fv.whale_contribution_raw] if fv.whale_contribution_raw else [],
        temperature=1.3,
        smart_money_factors={
            "fresh": [fv.sm_fresh_raw] if fv.sm_fresh_raw else [],
            "cluster": [fv.sm_cluster_raw] if fv.sm_cluster_raw else [],
            "registry": [fv.sm_registry_raw] if fv.sm_registry_raw else [],
        },
        oi_factor=fv.oi_factor_raw or 0.0,
        concentration_factor=fv.concentration_factor_raw or 0.0,
        micro_factor=fv.micro_factor_raw or 0.0,
        sports_odds_factor=0.0,
        retail_factor=0.0,
        maker_direction_factor=fv.maker_direction_raw or 0.0,
        taker_imbalance_factor=fv.taker_imbalance_raw or 0.0,
        calibration_method="temperature",
        beta_a=1.0,
        beta_b=0.0,
        category=fv.category,
    )
    return float(result["oracle_score"])


_WEIGHTS = {
    "w_mkt": 0.6165,
    "w_whale": 0.5,
    "w_whale_contra": 0.0,
    "w_fresh": 0.0,
    "w_cluster": 0.0,
    "w_registry": 0.1,
    "w_oi": 0.2,
    "w_concentration": 0.0,
    "w_micro": 0.0,
    "w_maker_direction": 0.5,
    "w_taker_imbalance": 0.0,
    "w_sports_odds": 0.0,
    "w_retail": 0.0,
}


@pytest.mark.parametrize(
    "fv",
    [
        _make_fv(0.3),
        _make_fv(0.5, whale_contribution_raw=0.4),
        _make_fv(0.7, maker_direction_raw=-0.3),
        _make_fv(0.5, whale_contribution_raw=0.2, sm_registry_raw=0.5),
        _make_fv(0.1, whale_contribution_raw=0.6, oi_factor_raw=0.3),
    ],
)
def test_pure_matches_legacy(fv):
    pure = score(fv, ScoringConfig(label="t", weights=_WEIGHTS))
    legacy = _legacy_score(fv, _WEIGHTS)
    assert abs(pure.oracle_score - legacy) < _TOLERANCE, (
        f"Divergence: pure={pure.oracle_score:.5f} legacy={legacy:.5f} for fv={fv}"
    )
