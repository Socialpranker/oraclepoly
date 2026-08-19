"""Тест что live_readiness использует virtual period данные, а не shadow_7d."""
import inspect

import app.api.bot as bot_module


def test_live_readiness_uses_virtual_period_not_shadow_7d():
    """bot_v2_lifecycle_live_readiness не должен вызывать fetch_shadow_signals_7d.

    Причина: shadow_7d смотрит последние 7 дней от NOW() без привязки к promoted_at.
    Correct источник — fetch_virtual_period_signals (весь virtual period).
    """
    source = inspect.getsource(bot_module.bot_v2_lifecycle_live_readiness)
    assert "fetch_shadow_signals_7d" not in source, (
        "bot_v2_lifecycle_live_readiness использует fetch_shadow_signals_7d — "
        "должен использовать fetch_virtual_period_signals"
    )
    assert "fetch_virtual_period_signals" in source, (
        "bot_v2_lifecycle_live_readiness должен использовать fetch_virtual_period_signals"
    )
