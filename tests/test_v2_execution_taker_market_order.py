"""Regression: taker-ордер должен идти через market-order метод CLOB.

Инцидент 2026-06-28: все 11 реальных live-ордеров D3 (taker, FOK) отбиты
CLOB с `invalid price (0), min: 0.01 - max: 0.99` — client-side, до сети.

Причина: taker-ветка `_place_order` строила `MarketOrderArgs(... без price)` и
слала через `create_and_post_order`. Этот метод роутится в `create_order`
(LIMIT-билдер), который читает `order_args.price` напрямую (дефолт 0) и роняет
валидацию. Авто-вывод рыночной цены из стакана живёт ТОЛЬКО в
`create_market_order` / `create_and_post_market_order`.

Тесты:
  1. taker → вызывается create_and_post_market_order (НЕ create_and_post_order);
  2. maker → по-прежнему create_and_post_order (регрессия не сломана);
  3. guard: реальный py_clob_client_v2 всё ещё экспортирует market-метод.
"""

from __future__ import annotations

import pytest

from app.core.config import Settings


class _RecordingClient:
    """Фейковый CLOB-клиент: пишет, какой order-метод и с чем был вызван."""

    def __init__(self):
        self.calls: list[tuple[str, dict]] = []

    def set_api_creds(self, _creds):  # noqa: D401 — клиент требует этот вызов
        pass

    def create_and_post_order(self, **kwargs):
        self.calls.append(("create_and_post_order", kwargs))
        return {"success": True, "orderID": "limit-oid"}

    def create_and_post_market_order(self, **kwargs):
        self.calls.append(("create_and_post_market_order", kwargs))
        return {"success": True, "orderID": "market-oid"}


def _make_settings(**kwargs) -> Settings:
    defaults = dict(
        database_url="postgresql://x:x@localhost/x",
        openai_api_key="sk-x",
        redis_url="redis://localhost",
        telegram_bot_token="1:x",
        admin_telegram_ids=[1],
        admin_api_key="x",
        polymarket_api_key="x",
        polymarket_api_secret="y",
        polymarket_api_passphrase="z",
    )
    defaults.update(kwargs)
    return Settings(**defaults)


@pytest.fixture
def patched_place_order(monkeypatch):
    """Подменяет PolymarketService.clob_client на recording-клиент + settings."""
    import app.jobs.v2_execution as v2

    rec = _RecordingClient()

    class _FakeSvc:
        clob_client = rec

    monkeypatch.setattr(v2, "get_settings", lambda: _make_settings())
    monkeypatch.setattr(
        "app.services.polymarket_service.PolymarketService", lambda: _FakeSvc()
    )
    return v2, rec


@pytest.mark.asyncio
async def test_taker_uses_market_order_method(patched_place_order):
    """Taker (FOK market) → create_and_post_market_order, чтобы цена выводилась из книги."""
    v2, rec = patched_place_order
    signal = {"token_id": "tok-abc", "stake": 5.47, "side": "NO"}

    await v2._place_order(signal, mode="taker", limit_price=None)

    methods = [name for name, _ in rec.calls]
    assert methods == ["create_and_post_market_order"], (
        f"taker должен звать market-метод, а не limit; вызвано: {methods}"
    )


@pytest.mark.asyncio
async def test_maker_still_uses_limit_order_method(patched_place_order):
    """Maker (GTC limit) → create_and_post_order (регрессия не сломана)."""
    v2, rec = patched_place_order
    signal = {"token_id": "tok-abc", "stake": 5.47, "side": "YES"}

    await v2._place_order(signal, mode="maker", limit_price=0.49)

    methods = [name for name, _ in rec.calls]
    assert methods == ["create_and_post_order"], (
        f"maker должен звать limit-метод; вызвано: {methods}"
    )


def test_library_exposes_market_order_method():
    """Guard: py_clob_client_v2 экспортирует create_and_post_market_order.

    Падёт, если обновление либы переименует/удалит метод — тогда фикс надо чинить.
    """
    from py_clob_client_v2 import ClobClient

    assert hasattr(ClobClient, "create_and_post_market_order"), (
        "py_clob_client_v2.ClobClient больше не имеет create_and_post_market_order"
    )


@pytest.mark.asyncio
async def test_taker_order_carries_price_cap(patched_place_order):
    """price_cap>0 передаётся в MarketOrderArgs.price (audit 2026-07-02 топ-2:
    FOK не должен безусловно уходить вглубь книги — потолок защищает от slippage,
    аналогично тому как явный price=0 когда-то ронял ордер, здесь явный price>0
    его ограничивает)."""
    v2, rec = patched_place_order
    signal = {"token_id": "tok-abc", "stake": 5.47, "side": "NO"}

    await v2._place_order(signal, mode="taker", limit_price=None, price_cap=0.55)

    _, kwargs = rec.calls[0]
    assert kwargs["order_args"].price == 0.55


@pytest.mark.asyncio
async def test_taker_order_without_price_cap_defaults_to_zero(patched_place_order):
    """price_cap=None (дефолт) — MarketOrderArgs.price остаётся 0 (auto-calculated
    самим py_clob_client_v2 из книги), поведение до этого фикса не меняется."""
    v2, rec = patched_place_order
    signal = {"token_id": "tok-abc", "stake": 5.47, "side": "NO"}

    await v2._place_order(signal, mode="taker", limit_price=None)

    _, kwargs = rec.calls[0]
    assert kwargs["order_args"].price == 0


def test_price_cap_formula():
    from app.jobs.v2_execution import _taker_price_cap

    # живой ask 0.50, сигнальная цена 0.48 (зона MIDLOW: entry 0.30-0.50 → drift-допуск 30%)
    cap = _taker_price_cap(current_ask=0.50, signal_price=0.48)
    assert cap > 0
    assert cap <= round(0.48 * 1.30, 2)  # не дальше зонального допуска от сигнальной цены
    assert cap >= 0.50  # не ниже живого ask (иначе FOK гарантированно не исполнится)


def test_price_cap_never_zero_even_for_tiny_price():
    from app.jobs.v2_execution import _taker_price_cap

    cap = _taker_price_cap(current_ask=0.0, signal_price=0.0)
    assert cap == 0.01  # tick_size floor, инцидент price=0 2026-06-28 не повторяется
