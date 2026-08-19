"""Tests for shadow_scoring service — runs pure + legacy in parallel, logs diff."""

from datetime import UTC, datetime
from unittest.mock import MagicMock, patch

import pytest

from app.models.feature_vector import FeatureVector
from app.services.shadow_scoring import compare_scores_for_snapshot


def _fv(**kwargs):
    return FeatureVector(
        market_id="m",
        snapshot_at=datetime(2026, 4, 20, tzinfo=UTC),
        side="YES",
        market_price=0.5,
        source="live",
        **kwargs,
    )


@pytest.mark.asyncio
async def test_compare_logs_when_diff_exceeds_threshold(caplog):
    fv = _fv(whale_contribution_raw=0.5)
    # simulate legacy=0.60, pure=0.80 (diff=0.20 ≫ 1e-6 threshold)
    with (
        patch("app.services.shadow_scoring.score") as mock_pure,
    ):
        mock_pure.return_value = MagicMock(oracle_score=0.80, config_hash="abcd1234", components_used={"whale"})
        result = await compare_scores_for_snapshot(fv, legacy_oracle_score=0.60)

    assert result["diverges"] is True
    assert result["diff"] == pytest.approx(0.20, abs=1e-6)
    assert result["signed_diff"] == pytest.approx(0.20, abs=1e-6)


@pytest.mark.asyncio
async def test_compare_does_not_flag_tiny_diff():
    """Diff well below 1e-6 threshold → shadow_match."""
    fv = _fv()
    with patch("app.services.shadow_scoring.score") as mock_pure:
        mock_pure.return_value = MagicMock(oracle_score=0.5000000001, config_hash="h", components_used=set())
        result = await compare_scores_for_snapshot(fv, legacy_oracle_score=0.5)
    assert result["diverges"] is False


@pytest.mark.asyncio
async def test_compare_signed_diff_preserves_sign():
    """signed_diff is pure - legacy, so negative when pure < legacy."""
    fv = _fv()
    with patch("app.services.shadow_scoring.score") as mock_pure:
        mock_pure.return_value = MagicMock(oracle_score=0.40, config_hash="h", components_used=set())
        result = await compare_scores_for_snapshot(fv, legacy_oracle_score=0.60)
    assert result["signed_diff"] == pytest.approx(-0.20, abs=1e-6)
    assert result["diff"] == pytest.approx(0.20, abs=1e-6)
