"""Контракт безопасности авто-сброса sticky kill-switch (инцидент 2026-06-28).

Транзиентные причины (anomalies/exposure/daily_pnl/drift) снимаются после N
чистых циклов. Persistent-фолты (main_alive/recon_strikes/main_self_check) и
manual-значения НИКОГДА не снимаются автоматически.
"""

from __future__ import annotations

import pytest

from app.services.kill_switch_clear import (
    KILL_SWITCH_KEY,
    is_auto_clearable,
    reset_clean_streak,
    try_auto_clear,
)


class FakeRedis:
    """In-memory Redis-заглушка для streak/flag логики."""

    def __init__(self, initial: dict | None = None):
        self.store: dict[str, str] = dict(initial or {})

    async def incr(self, key: str) -> int:
        val = int(self.store.get(key, 0)) + 1
        self.store[key] = str(val)
        return val

    async def expire(self, key: str, ttl: int) -> None:
        pass

    async def get(self, key: str):
        return self.store.get(key)

    async def delete(self, *keys: str) -> None:
        for k in keys:
            self.store.pop(k, None)


STREAK = "bot:test_clean_streak"

# --- is_auto_clearable ----------------------------------------------------


@pytest.mark.parametrize(
    "value,expected",
    [
        # ТОЛЬКО reconciliation_drift авто-снимается.
        ("reconciliation_drift:trades_error=...:strikes=3", True),
        # Всё остальное sticky/manual:
        ("daemon:anomalies:anomaly_high_order_rate:21/min", False),  # код-фолт
        ("daemon:exposure:exposure_hard_cap:100.0%>60%", False),  # stale-знаменатель
        ("daemon:daily_pnl_hard:daily_loss_hard_kill:...", False),  # stale-MTM
        ("daemon:main_alive:main_heartbeat_stale:age=400s", False),
        ("daemon:recon_strikes:recon_strikes:3", False),
        ("main_self_check:risk_daemon_dead:age=400s", False),
        ("drill_test", False),
        ("1", False),
        (None, False),
        ("", False),
    ],
)
def test_is_auto_clearable_allowlist(value, expected):
    assert is_auto_clearable(value) is expected


# --- try_auto_clear -------------------------------------------------------


@pytest.mark.asyncio
async def test_transient_cleared_after_threshold():
    """reconciliation_drift снимается ровно на N-м чистом цикле."""
    r = FakeRedis({KILL_SWITCH_KEY: "reconciliation_drift:trades_error=x:strikes=3"})
    # циклы 1..4 — рано
    for _ in range(4):
        assert await try_auto_clear(r, streak_key=STREAK, threshold=5) is False
        assert KILL_SWITCH_KEY in r.store
    # 5-й цикл — снимаем
    assert await try_auto_clear(r, streak_key=STREAK, threshold=5) is True
    assert KILL_SWITCH_KEY not in r.store
    assert STREAK not in r.store


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "sticky_value",
    [
        "daemon:main_alive:main_heartbeat_stale:age=400s",
        "main_self_check:risk_daemon_dead:age=400s",
        "daemon:recon_strikes:recon_strikes:3",
        "daemon:anomalies:anomaly_high_order_rate:21/min",  # код-фолт
        "daemon:exposure:exposure_hard_cap:100.0%>60%",  # stale-знаменатель
        "daemon:daily_pnl_hard:daily_loss_hard_kill:...",  # stale-MTM
        "drill_test",
    ],
)
async def test_persistent_fault_never_cleared(sticky_value):
    """Persistent/manual причины не снимаются даже при длинном streak."""
    r = FakeRedis({KILL_SWITCH_KEY: sticky_value})
    for _ in range(10):
        assert await try_auto_clear(r, streak_key=STREAK, threshold=5) is False
    assert r.store[KILL_SWITCH_KEY] == sticky_value


@pytest.mark.asyncio
async def test_no_latched_flag_is_noop():
    """Флага нет → ничего не снимаем, streak обнуляется."""
    r = FakeRedis()  # без kill_switch
    for _ in range(5):
        assert await try_auto_clear(r, streak_key=STREAK, threshold=5) is False
    assert KILL_SWITCH_KEY not in r.store
    assert STREAK not in r.store  # обнулён на здоровом пути


@pytest.mark.asyncio
async def test_reset_clean_streak_clears_counter():
    """reset_clean_streak обнуляет streak (вызов на НЕ-чистом цикле)."""
    r = FakeRedis({STREAK: "3"})
    await reset_clean_streak(r, STREAK)
    assert STREAK not in r.store


@pytest.mark.asyncio
async def test_redis_none_is_safe_noop():
    """Redis down → no-op, не падаем."""
    assert await try_auto_clear(None, streak_key=STREAK, threshold=5) is False
    await reset_clean_streak(None, STREAK)  # не падает


@pytest.mark.asyncio
async def test_owned_prefixes_skips_other_writers_flag():
    """owned_prefixes блокирует снятие auto-clearable флага вне своей зоны.

    Флаг reconciliation_drift В allowlist, но owned_prefixes его НЕ покрывает →
    не снимаем (defense-in-depth: чужой writer не трогает не свой флаг).
    """
    r = FakeRedis({KILL_SWITCH_KEY: "reconciliation_drift:trades_error=x:strikes=3"})
    for _ in range(6):
        cleared = await try_auto_clear(
            r, streak_key=STREAK, threshold=5,
            owned_prefixes=("daemon:exposure",),
        )
        assert cleared is False
    assert KILL_SWITCH_KEY in r.store


@pytest.mark.asyncio
async def test_owned_prefixes_clears_own_flag():
    """С owned_prefixes свой транзиентный флаг снимается."""
    r = FakeRedis({KILL_SWITCH_KEY: "reconciliation_drift:trades_error=x:strikes=3"})
    cleared = False
    for _ in range(5):
        cleared = await try_auto_clear(
            r, streak_key=STREAK, threshold=5,
            owned_prefixes=("reconciliation_drift:",),
        )
    assert cleared is True
    assert KILL_SWITCH_KEY not in r.store
