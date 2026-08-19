"""find_our_open_order матчинг: только asset_id недостаточен — на том же token_id
может висеть чужой неродственный resting-ордер (напр. maker GTC от предыдущего
сигнала), и honest-сверка ошибочно припишет его текущей попытке (double-counted
exposure, битый PnL при резолве). Тесты покрывают false-positive (старый
неродственный ордер существует) и honest-recovery (наш ордер реально там)."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from app.services.clob_order_lookup import find_our_open_order


class _FakeClient:
    def __init__(self, orders):
        self._orders = orders

    def get_open_orders(self, params):
        return self._orders


@pytest.mark.asyncio
async def test_ignores_unrelated_stale_order_on_same_token_wrong_size():
    """Старый resting-ордер на том же token_id с другим size — не наш, должен
    быть отфильтрован по expected_size сверке."""
    now = datetime.now(UTC)
    stale_unrelated_order = {
        "id": "stale-unrelated-order",
        "asset_id": "tok-abc",
        "original_size": 500.0,
        "created_at": (now - timedelta(hours=2)).isoformat(),
    }
    client = _FakeClient([stale_unrelated_order])

    result = await find_our_open_order(
        client,
        "tok-abc",
        expected_size=12.5,
        not_before=now - timedelta(minutes=1),
    )

    assert result is None


@pytest.mark.asyncio
async def test_ignores_unrelated_stale_order_on_same_token_created_before_attempt():
    """Старый resting-ордер created_at раньше not_before (момента вызова
    _place_order) — не наш, должен быть отфильтрован по времени, даже если
    размер случайно совпал."""
    now = datetime.now(UTC)
    stale_unrelated_order = {
        "id": "stale-unrelated-order",
        "asset_id": "tok-abc",
        "original_size": 12.5,
        "created_at": (now - timedelta(hours=2)).isoformat(),
    }
    client = _FakeClient([stale_unrelated_order])

    result = await find_our_open_order(
        client,
        "tok-abc",
        expected_size=12.5,
        not_before=now - timedelta(minutes=1),
    )

    assert result is None


@pytest.mark.asyncio
async def test_recovers_our_order_matching_size_and_created_after_attempt():
    """Honest-recovery: наш ордер реально размещён (POST дошёл до сервера,
    ответ до нас — нет), created_at после not_before, size совпадает."""
    now = datetime.now(UTC)
    our_order = {
        "id": "our-real-order",
        "asset_id": "tok-abc",
        "original_size": 12.5,
        "created_at": (now + timedelta(seconds=2)).isoformat(),
    }
    client = _FakeClient([our_order])

    result = await find_our_open_order(
        client,
        "tok-abc",
        expected_size=12.5,
        not_before=now,
    )

    assert result == "our-real-order"


@pytest.mark.asyncio
async def test_recovers_our_order_within_size_tolerance():
    """Небольшое расхождение size (округление на бирже) внутри tolerance —
    всё ещё матчится."""
    now = datetime.now(UTC)
    our_order = {
        "id": "our-real-order",
        "asset_id": "tok-abc",
        "original_size": 12.6,  # 0.8% отклонение от expected_size=12.5
        "created_at": (now + timedelta(seconds=2)).isoformat(),
    }
    client = _FakeClient([our_order])

    result = await find_our_open_order(
        client,
        "tok-abc",
        expected_size=12.5,
        not_before=now,
    )

    assert result == "our-real-order"


@pytest.mark.asyncio
async def test_picks_our_order_over_unrelated_order_on_same_token():
    """Оба ордера на одном token_id: чужой старый (другой size, раньше
    not_before) и наш (совпадает size, после not_before) — должен вернуться наш,
    не самый свежий по created_at слепо."""
    now = datetime.now(UTC)
    unrelated_old = {
        "id": "unrelated-old-order",
        "asset_id": "tok-abc",
        "original_size": 500.0,
        "created_at": (now - timedelta(hours=2)).isoformat(),
    }
    ours = {
        "id": "our-real-order",
        "asset_id": "tok-abc",
        "original_size": 12.5,
        "created_at": (now + timedelta(seconds=2)).isoformat(),
    }
    client = _FakeClient([unrelated_old, ours])

    result = await find_our_open_order(
        client,
        "tok-abc",
        expected_size=12.5,
        not_before=now,
    )

    assert result == "our-real-order"


@pytest.mark.asyncio
async def test_backward_compatible_without_matching_params_returns_most_recent():
    """Без expected_size/not_before (старые вызовы) — поведение как раньше:
    самый свежий открытый ордер по asset_id."""
    now = datetime.now(UTC)
    older = {
        "id": "older-order",
        "asset_id": "tok-abc",
        "original_size": 5.0,
        "created_at": (now - timedelta(minutes=5)).isoformat(),
    }
    newer = {
        "id": "newer-order",
        "asset_id": "tok-abc",
        "original_size": 5.0,
        "created_at": now.isoformat(),
    }
    client = _FakeClient([older, newer])

    result = await find_our_open_order(client, "tok-abc")

    assert result == "newer-order"


@pytest.mark.asyncio
async def test_ignores_order_on_different_token():
    client = _FakeClient(
        [{"id": "other-token-order", "asset_id": "tok-other", "original_size": 12.5, "created_at": datetime.now(UTC).isoformat()}]
    )

    result = await find_our_open_order(client, "tok-abc", expected_size=12.5)

    assert result is None
