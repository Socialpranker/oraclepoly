"""Тесты конфигурации execution после рефакторинга live gate SSoT."""

from app.core.config import Settings


def _make_settings(**kwargs) -> Settings:
    defaults = dict(
        database_url="postgresql://x:x@localhost/x",
        openai_api_key="sk-x",
        redis_url="redis://localhost",
        telegram_bot_token="1:x",
        admin_telegram_ids=[1],
        admin_api_key="x",
        polymarket_api_key="x",
    )
    defaults.update(kwargs)
    return Settings(**defaults)


def test_no_execution_live_field():
    """execution_live убран — SSoT теперь strategy_state.stage в БД."""
    s = _make_settings()
    assert not hasattr(s, "execution_live"), "execution_live должен быть удалён"


def test_no_execution_dry_run_field():
    """execution_dry_run убран — dry/live определяется через strategy_state.stage."""
    s = _make_settings()
    assert not hasattr(s, "execution_dry_run"), "execution_dry_run должен быть удалён"


def test_no_live_strategies_field():
    """live_strategies убран — список live стратегий только в strategy_state БД."""
    s = _make_settings()
    assert not hasattr(s, "live_strategies"), "live_strategies должен быть удалён"


def test_execution_enabled_still_exists():
    """execution_enabled остаётся — глобальный kill switch."""
    s = _make_settings()
    assert hasattr(s, "execution_enabled")


def test_no_legacy_live_fields():
    """Legacy env-поля удалены — stake/limits через Adaptive Kelly Sizing (strategy_live_metrics).

    live_strategies_default: удалён — включение через POST /bot/strategies/{key}/toggle.
    live_initial_bankroll: удалён — bankroll = real wallet_balance.
    live_daily_limits: удалён — per-strategy limits adaptive.
    execution_strategies: удалён — SSoT strategy_state.stage='live'.
    """
    s = _make_settings()
    for deprecated in ("live_strategies_default", "live_initial_bankroll", "live_daily_limits", "execution_strategies"):
        assert not hasattr(s, deprecated), f"{deprecated} должен быть удалён"
