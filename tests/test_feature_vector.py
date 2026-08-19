"""Tests for FeatureVector and ScoringConfig Pydantic models."""

from datetime import UTC, datetime

import pytest

from app.models.feature_vector import FeatureVector, ScoringConfig


def test_feature_vector_minimal_valid():
    """A FeatureVector with only required fields validates."""
    fv = FeatureVector(
        market_id="abc123",
        snapshot_at=datetime(2026, 4, 20, tzinfo=UTC),
        side="YES",
        market_price=0.55,
        source="live",
    )
    assert fv.market_id == "abc123"
    assert fv.side == "YES"
    assert fv.components_available == set()


def test_feature_vector_side_must_be_yes_or_no():
    """side='INVALID' raises ValidationError."""
    with pytest.raises(Exception):  # ValidationError
        FeatureVector(
            market_id="abc",
            snapshot_at=datetime.now(UTC),
            side="INVALID",
            market_price=0.5,
            source="live",
        )


def test_feature_vector_source_must_be_valid():
    """source='random' raises ValidationError."""
    with pytest.raises(Exception):
        FeatureVector(
            market_id="abc",
            snapshot_at=datetime.now(UTC),
            side="YES",
            market_price=0.5,
            source="random",
        )


def test_feature_vector_components_derived_from_non_null():
    """components_available is derived from which *_raw fields are set."""
    fv = FeatureVector(
        market_id="abc",
        snapshot_at=datetime.now(UTC),
        side="YES",
        market_price=0.5,
        source="live",
        whale_contribution_raw=0.3,
        news_contribution_raw=0.1,
        maker_direction_raw=-0.2,
    )
    assert "whale" in fv.components_available
    assert "news" in fv.components_available
    assert "maker" in fv.components_available
    assert "taker" not in fv.components_available


def test_scoring_config_hash_deterministic():
    """Same inputs yield same hash."""
    a = ScoringConfig(
        label="test",
        weights={"w_whale": 0.8, "w_news": 0.5},
    )
    b = ScoringConfig(
        label="different_label",
        weights={"w_news": 0.5, "w_whale": 0.8},  # different key order
    )
    assert a.hash == b.hash


def test_scoring_config_hash_changes_with_weights():
    """Different weights → different hash."""
    a = ScoringConfig(label="a", weights={"w_whale": 0.8})
    b = ScoringConfig(label="a", weights={"w_whale": 0.9})
    assert a.hash != b.hash


def test_scoring_config_hash_changes_with_flags():
    """Different formula_flags → different hash."""
    a = ScoringConfig(label="a", weights={"w": 1.0}, formula_flags={"mode": "a"})
    b = ScoringConfig(label="a", weights={"w": 1.0}, formula_flags={"mode": "b"})
    assert a.hash != b.hash


def test_scoring_config_hash_is_16_chars():
    """Hash is 16-char truncated md5 hex."""
    c = ScoringConfig(label="t", weights={"w": 1.0})
    assert len(c.hash) == 16
    assert all(ch in "0123456789abcdef" for ch in c.hash)
