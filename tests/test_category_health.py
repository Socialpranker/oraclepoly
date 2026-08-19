"""Тесты category_health: материализация blacklist-кандидатов в Redis
и fail-open shadow-lookup в v2_execution.

Гейт НЕ блокирует (shadow-паттерн D3 Sports-блэклиста): джоба пишет кандидатов
в Redis, execution-путь на кандидате только логирует would-block в gate_shadow_log.
Money-path не должен зависеть от телеметрии ни при каком сбое (fail-open).
"""

from __future__ import annotations

import json
from unittest.mock import AsyncMock

import pytest

import app.jobs.category_health as ch_mod
from app.jobs.v2_execution import _category_health_candidate


def _row(detector, category, events, roi, med):
    return {"detector": detector, "category": category, "events": events, "roi": roi, "median_event_roi": med}


class _FakeRedis:
    def __init__(self):
        self.store: dict[str, str] = {}
        self.ttl: dict[str, int] = {}

    async def set(self, key, value, ex=None):
        self.store[key] = value
        if ex is not None:
            self.ttl[key] = ex
        return True

    async def get(self, key):
        return self.store.get(key)


@pytest.mark.asyncio
async def test_refresh_materializes_candidates(monkeypatch):
    redis = _FakeRedis()

    async def _fake_fetch(sql, *args):
        return [_row("D42_event_driven_ml", "Sports", 254, -0.023, -0.276)]

    monkeypatch.setattr(ch_mod, "fetch", _fake_fetch)
    monkeypatch.setattr(ch_mod, "get_redis", AsyncMock(return_value=redis))

    result = await ch_mod.run_category_health()

    assert result == {"status": "ok", "candidates": 1, "cells": ["D42_event_driven_ml:Sports"]}
    stored = json.loads(redis.store[ch_mod.CANDIDATES_KEY])
    assert stored["D42_event_driven_ml:Sports"] == {"events": 254, "roi": -0.023, "median_event_roi": -0.276}
    assert redis.ttl[ch_mod.CANDIDATES_KEY] == ch_mod._TTL_SEC


@pytest.mark.asyncio
async def test_refresh_empty_candidates_writes_empty_dict(monkeypatch):
    """Нет кандидатов → пишем пустой dict (а не пропускаем запись): протухший
    ключ со старыми кандидатами хуже пустого."""
    redis = _FakeRedis()

    async def _fake_fetch(sql, *args):
        return []

    monkeypatch.setattr(ch_mod, "fetch", _fake_fetch)
    monkeypatch.setattr(ch_mod, "get_redis", AsyncMock(return_value=redis))

    result = await ch_mod.run_category_health()

    assert result["status"] == "ok" and result["candidates"] == 0
    assert json.loads(redis.store[ch_mod.CANDIDATES_KEY]) == {}


@pytest.mark.asyncio
async def test_refresh_redis_unavailable_reports_error(monkeypatch):
    async def _fake_fetch(sql, *args):
        return [_row("D42_event_driven_ml", "Sports", 254, -0.023, -0.276)]

    monkeypatch.setattr(ch_mod, "fetch", _fake_fetch)
    monkeypatch.setattr(ch_mod, "get_redis", AsyncMock(return_value=None))

    result = await ch_mod.run_category_health()

    assert result["status"] == "error"


def test_candidates_sql_rule():
    """Правило кандидата: только virtual, events>=30, оба ROI-знака отрицательны."""
    sql = ch_mod._CANDIDATES_SQL
    assert "mode = 'virtual'" in sql
    assert "events >= 30" in sql
    assert "roi < 0" in sql
    assert "median_event_roi < 0" in sql


@pytest.mark.asyncio
async def test_candidate_lookup_hit_and_miss(monkeypatch):
    redis = _FakeRedis()
    redis.store[ch_mod.CANDIDATES_KEY] = json.dumps({"D42_event_driven_ml:Sports": {}})
    monkeypatch.setattr("app.core.cache.get_redis", AsyncMock(return_value=redis))

    assert await _category_health_candidate("D42_event_driven_ml", "Sports") is True
    assert await _category_health_candidate("D42_event_driven_ml", "Crypto") is False
    assert await _category_health_candidate("D3_cross_mkt_arb", "Sports") is False
    assert await _category_health_candidate("D42_event_driven_ml", None) is False


@pytest.mark.asyncio
async def test_candidate_lookup_fail_open(monkeypatch):
    # Redis недоступен
    monkeypatch.setattr("app.core.cache.get_redis", AsyncMock(return_value=None))
    assert await _category_health_candidate("D42_event_driven_ml", "Sports") is False

    # ключа нет
    monkeypatch.setattr("app.core.cache.get_redis", AsyncMock(return_value=_FakeRedis()))
    assert await _category_health_candidate("D42_event_driven_ml", "Sports") is False

    # битый JSON
    broken = _FakeRedis()
    broken.store[ch_mod.CANDIDATES_KEY] = "{not json"
    monkeypatch.setattr("app.core.cache.get_redis", AsyncMock(return_value=broken))
    assert await _category_health_candidate("D42_event_driven_ml", "Sports") is False

    # get_redis бросает
    monkeypatch.setattr("app.core.cache.get_redis", AsyncMock(side_effect=RuntimeError("redis down")))
    assert await _category_health_candidate("D42_event_driven_ml", "Sports") is False
