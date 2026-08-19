"""Tests для PIPELINE_VERSION config flag (Stage 4)."""

from __future__ import annotations

import pytest


def test_default_is_monolith(monkeypatch):
    monkeypatch.delenv("PIPELINE_VERSION", raising=False)
    from app.core.config import get_settings

    get_settings.cache_clear()
    settings = get_settings()
    assert settings.pipeline_version == "monolith"


def test_v2_two_stage_accepted(monkeypatch):
    monkeypatch.setenv("PIPELINE_VERSION", "v2_two_stage")
    from app.core.config import get_settings

    get_settings.cache_clear()
    settings = get_settings()
    assert settings.pipeline_version == "v2_two_stage"
    get_settings.cache_clear()


def test_invalid_value_raises(monkeypatch):
    monkeypatch.setenv("PIPELINE_VERSION", "invalid_value")
    from app.core.config import get_settings

    get_settings.cache_clear()
    with pytest.raises(Exception):  # pydantic ValidationError
        get_settings()
    monkeypatch.delenv("PIPELINE_VERSION", raising=False)
    get_settings.cache_clear()
