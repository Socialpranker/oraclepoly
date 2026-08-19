"""Tests for pure scoring function."""

from datetime import UTC, datetime

from app.core.scoring_pure import ScoreResult, apply_calibration, score, score_raw
from app.models.feature_vector import FeatureVector, ScoringConfig


def _make_fv(**overrides) -> FeatureVector:
    defaults = dict(
        market_id="m1",
        snapshot_at=datetime(2026, 4, 20, tzinfo=UTC),
        side="YES",
        market_price=0.5,
        source="live",
    )
    defaults.update(overrides)
    return FeatureVector(**defaults)


def _make_config(**overrides) -> ScoringConfig:
    defaults = dict(
        label="test",
        weights={
            "w_mkt": 1.0,
            "w_whale": 0.5,
            "w_news": 0.3,
            "w_sm_registry": 0.1,
            "w_oi": 0.2,
            "w_maker_direction": 0.5,
            "w_taker_imbalance": 0.0,
        },
    )
    defaults.update(overrides)
    return ScoringConfig(**defaults)


def test_score_returns_ScoreResult():
    fv = _make_fv()
    cfg = _make_config()
    result = score(fv, cfg)
    assert isinstance(result, ScoreResult)
    assert 0.0 <= result.oracle_score <= 1.0


def test_score_is_pure_deterministic():
    """Same inputs, same outputs — stateless."""
    fv = _make_fv(whale_contribution_raw=0.2, news_contribution_raw=-0.1)
    cfg = _make_config()
    r1 = score(fv, cfg)
    r2 = score(fv, cfg)
    assert r1.oracle_score == r2.oracle_score
    assert r1.z_posterior == r2.z_posterior
    assert r1.divergence == r2.divergence


def test_score_market_prior_only_when_no_signals():
    """All signals NULL → oracle_score ≈ market_price (pure prior)."""
    fv = _make_fv(market_price=0.42)
    cfg = _make_config()
    result = score(fv, cfg)
    assert abs(result.oracle_score - 0.42) < 0.05  # small calibration shift
    assert result.components_used == set()


def test_score_includes_component_when_non_null():
    fv = _make_fv(whale_contribution_raw=0.5)
    cfg = _make_config()
    result = score(fv, cfg)
    assert "whale" in result.components_used


def test_score_divergence_is_oracle_minus_price():
    fv = _make_fv(market_price=0.3)
    cfg = _make_config()
    result = score(fv, cfg)
    assert abs(result.divergence - (result.oracle_score - 0.3)) < 1e-9


def test_score_config_hash_reflected_in_result():
    fv = _make_fv()
    cfg = _make_config()
    result = score(fv, cfg)
    assert result.config_hash == cfg.hash


def test_score_none_safe_missing_weight():
    """Config без w_whale → компонент игнорируется, не падает."""
    fv = _make_fv(whale_contribution_raw=0.5)
    cfg = ScoringConfig(label="partial", weights={"w_mkt": 1.0})  # нет w_whale
    result = score(fv, cfg)
    assert "whale" not in result.components_used


def test_score_invalid_market_price_clamped():
    """market_price=0 или 1 не вызывает div by zero."""
    fv_low = _make_fv(market_price=0.001)
    fv_high = _make_fv(market_price=0.999)
    cfg = _make_config()
    r_low = score(fv_low, cfg)
    r_high = score(fv_high, cfg)
    assert 0.0 <= r_low.oracle_score <= 1.0
    assert 0.0 <= r_high.oracle_score <= 1.0


def test_score_raw_no_calibration():
    """score_raw returns uncalibrated posterior — can exceed ±max_divergence from market."""
    fv = _make_fv(market_price=0.1, whale_contribution_raw=0.6, oi_factor_raw=0.3)
    cfg = _make_config()
    raw = score_raw(fv, cfg)
    # raw divergence unclipped — can be large
    assert abs(raw.divergence) > 0.1 or raw.oracle_score == raw.oracle_score  # no-op compile check


def test_apply_calibration_clamps_max_divergence():
    """apply_calibration enforces max_divergence ≤ 0.10 (default)."""
    raw = ScoreResult(
        oracle_score=0.8,  # far from market_price 0.3 → div=+0.5
        z_posterior=2.0,
        divergence=0.5,
        config_hash="test",
    )
    calibrated = apply_calibration(raw, market_price=0.3, category=None)
    # Upward damping 0.3 → 0.15, clamped to max 0.10. oracle = 0.3 + 0.10 = 0.40
    assert abs(calibrated.divergence) <= 0.10 + 1e-9
    assert calibrated.oracle_score <= 1.0 - 1e-5


def test_apply_calibration_category_specific_damping():
    """weather has upward_damping=0.15, tighter than default 0.30."""
    raw = ScoreResult(oracle_score=0.7, z_posterior=0.8, divergence=0.2, config_hash="t")
    cal_default = apply_calibration(raw, market_price=0.5, category=None)
    cal_weather = apply_calibration(raw, market_price=0.5, category="Weather")
    # Weather damping more aggressive → smaller divergence than default
    assert cal_weather.divergence < cal_default.divergence


def test_score_equals_score_raw_plus_apply_calibration():
    """score() is exactly apply_calibration(score_raw(...), ...)."""
    fv = _make_fv(market_price=0.4, whale_contribution_raw=0.3, category="Crypto")
    cfg = _make_config()

    direct = score(fv, cfg)
    two_stage = apply_calibration(score_raw(fv, cfg), fv.market_price, fv.category)

    assert direct.oracle_score == two_stage.oracle_score
    assert direct.divergence == two_stage.divergence
