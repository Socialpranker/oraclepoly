"""Tests для fetch_signals_for_active_pipeline router (Stage 4)."""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, patch


def test_router_monolith_path(monkeypatch):
    """PIPELINE_VERSION=monolith → fetch_calibrated_detector_signals."""
    monkeypatch.setenv("PIPELINE_VERSION", "monolith")
    from app.core.config import get_settings

    get_settings.cache_clear()

    with (
        patch(
            "app.services.detector_signal_service.fetch_calibrated_detector_signals",
            new=AsyncMock(return_value=[{"path": "monolith"}]),
        ) as mono,
        patch(
            "app.services.two_stage_inference.fetch_two_stage_calibrated_signals",
            new=AsyncMock(return_value=[{"path": "v2"}]),
        ) as v2,
    ):
        from app.services.detector_signal_service import fetch_signals_for_active_pipeline

        result = asyncio.run(fetch_signals_for_active_pipeline())
        assert result == [{"path": "monolith"}]
        mono.assert_awaited_once()
        v2.assert_not_awaited()
    get_settings.cache_clear()


def test_router_v2_path(monkeypatch):
    """PIPELINE_VERSION=v2_two_stage → fetch_two_stage_calibrated_signals."""
    monkeypatch.setenv("PIPELINE_VERSION", "v2_two_stage")
    from app.core.config import get_settings

    get_settings.cache_clear()

    with (
        patch(
            "app.services.detector_signal_service.fetch_calibrated_detector_signals",
            new=AsyncMock(return_value=[{"path": "monolith"}]),
        ) as mono,
        patch(
            "app.services.two_stage_inference.fetch_two_stage_calibrated_signals",
            new=AsyncMock(return_value=[{"path": "v2"}]),
        ) as v2,
    ):
        from app.services.detector_signal_service import fetch_signals_for_active_pipeline

        result = asyncio.run(fetch_signals_for_active_pipeline())
        assert result == [{"path": "v2"}]
        v2.assert_awaited_once()
        mono.assert_not_awaited()
    monkeypatch.delenv("PIPELINE_VERSION", raising=False)
    get_settings.cache_clear()
