"""D42 scan job — unit tests.

Мокаем _load_models (диск /mnt/data недоступен в CI), fetch (live_ml_features),
repo.insert, fetch_open_event_ids. Fake LGBM-модель возвращает заданный delta.

Патчим атрибуты УЖЕ импортированного модуля d42_scan (не ленивый импорт внутри
with) — иначе под pytest-xdist патч может не примениться к namespace функции.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest

from app.jobs import d42_scan


class _FakeModel:
    """Stub LGBM Regressor: predict() возвращает фикс. delta на каждую строку."""

    def __init__(self, delta: float):
        self._delta = delta

    def predict(self, X):
        return [self._delta] * len(X)


def _feature_cols() -> list[str]:
    return ["yes_price_at_anchor", "horizon_days"]


def _row(anchor_hours: int, yes_price: float, *, market_id: str = "0xCOND1", event_id: str = "ev1") -> dict:
    return {
        "market_id": market_id,
        "anchor_hours": anchor_hours,
        "yes_price_at_anchor": yes_price,
        "horizon_days": 3.0,
        "market_event_id": event_id,
        "market_category": "Crypto",
        "clob_token_ids": None,
    }


def _models(delta: float) -> dict:
    """Только 168_to_24 модель (anchor_from=168) для предсказуемости."""
    return {"168_to_24": {"model": _FakeModel(delta), "feature_cols": _feature_cols(), "version": "test_v1"}}


def _patches(*, models: dict, rows: list, open_events: set, repo):
    """Контекст-менеджеры патчей атрибутов d42_scan (явные, не ленивый импорт)."""
    return (
        patch.object(d42_scan, "_load_models", return_value=models),
        patch.object(d42_scan, "fetch", new=AsyncMock(return_value=rows)),
        patch.object(d42_scan, "fetch_open_event_ids", new=AsyncMock(return_value=open_events)),
        patch.object(d42_scan, "get_detector_signal_repository", return_value=repo),
    )


@pytest.mark.asyncio
async def test_emits_no_signal_when_negative_delta_above_threshold():
    """delta ≤ -0.05 + valid price → NO-сигнал вставлен (NO-only с 2026-06-21)."""
    repo = AsyncMock()
    repo.insert = AsyncMock(return_value=True)
    p1, p2, p3, p4 = _patches(models=_models(-0.10), rows=[_row(168, 0.40)], open_events=set(), repo=repo)
    with p1, p2, p3, p4:
        result = await d42_scan.run_d42_scan()

    assert result["signals"] == 1
    assert result["inserted"] == 1
    repo.insert.assert_awaited_once()
    sig = repo.insert.await_args.args[0]
    assert sig.detector == "D42_event_driven_ml"
    assert sig.side == "NO"


@pytest.mark.asyncio
async def test_positive_delta_emits_nothing():
    """YES-плечо отрезано: положительный delta выше порога → 0 сигналов."""
    repo = AsyncMock()
    p1, p2, p3, p4 = _patches(models=_models(0.10), rows=[_row(168, 0.40)], open_events=set(), repo=repo)
    with p1, p2, p3, p4:
        result = await d42_scan.run_d42_scan()

    assert result["signals"] == 0
    repo.insert.assert_not_awaited()


@pytest.mark.asyncio
async def test_no_signal_when_delta_below_threshold():
    """|delta| < 0.05 → detect() возвращает None → 0 сигналов."""
    repo = AsyncMock()
    p1, p2, p3, p4 = _patches(models=_models(0.01), rows=[_row(168, 0.40)], open_events=set(), repo=repo)
    with p1, p2, p3, p4:
        result = await d42_scan.run_d42_scan()

    assert result["signals"] == 0
    repo.insert.assert_not_awaited()


@pytest.mark.asyncio
async def test_skips_open_event():
    """event_id уже в open_events → skip, не вставляем."""
    repo = AsyncMock()
    p1, p2, p3, p4 = _patches(
        models=_models(0.10), rows=[_row(168, 0.40, event_id="ev_open")], open_events={"ev_open"}, repo=repo
    )
    with p1, p2, p3, p4:
        result = await d42_scan.run_d42_scan()

    assert result["signals"] == 0
    assert result["skipped_open_event"] == 1
    repo.insert.assert_not_awaited()


@pytest.mark.asyncio
async def test_skips_placeholder_price():
    """yes_price_at_anchor == 0.5 (broken book placeholder) → skip."""
    repo = AsyncMock()
    p1, p2, p3, p4 = _patches(models=_models(0.10), rows=[_row(168, 0.5)], open_events=set(), repo=repo)
    with p1, p2, p3, p4:
        result = await d42_scan.run_d42_scan()

    assert result["signals"] == 0
    repo.insert.assert_not_awaited()


@pytest.mark.asyncio
async def test_no_models_returns_early():
    """Нет моделей на диске → early return, fetch не вызывается."""
    mock_fetch = AsyncMock()
    with patch.object(d42_scan, "_load_models", return_value={}), patch.object(d42_scan, "fetch", new=mock_fetch):
        result = await d42_scan.run_d42_scan()

    assert result["inserted"] == 0
    assert result["windows_run"] == 0
    mock_fetch.assert_not_awaited()


@pytest.mark.asyncio
async def test_no_live_features_returns_early():
    """live_ml_features пуст → early return."""
    with (
        patch.object(d42_scan, "_load_models", return_value=_models(0.10)),
        patch.object(d42_scan, "fetch", new=AsyncMock(return_value=[])),
    ):
        result = await d42_scan.run_d42_scan()

    assert result["markets"] == 0
    assert result["inserted"] == 0


@pytest.mark.asyncio
async def test_negative_delta_emits_no_side():
    """delta < -0.05 → сторона NO."""
    repo = AsyncMock()
    repo.insert = AsyncMock(return_value=True)
    p1, p2, p3, p4 = _patches(models=_models(-0.10), rows=[_row(168, 0.40)], open_events=set(), repo=repo)
    with p1, p2, p3, p4:
        await d42_scan.run_d42_scan()

    sig = repo.insert.await_args.args[0]
    assert sig.side == "NO"


def test_normalize_event_id_handles_nan_and_types():
    """_normalize_event_id: NaN/None → None, иначе str.

    Корень CI-падения test_skips_open_event (py3.11): pandas мог отдать NaN/numpy
    вместо str → `event_id in open_events` ломался. Юнит-тест чистой функции, НЕ
    зависит от pandas-row/версии.
    """
    import numpy as _np

    assert d42_scan._normalize_event_id(None) is None
    assert d42_scan._normalize_event_id(float("nan")) is None
    assert d42_scan._normalize_event_id(_np.nan) is None
    assert d42_scan._normalize_event_id("ev_open") == "ev_open"
    assert d42_scan._normalize_event_id(_np.str_("ev1")) == "ev1"
    assert d42_scan._normalize_event_id(_np.int64(42)) == "42"
