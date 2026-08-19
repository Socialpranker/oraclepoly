"""Tests for virtual early-exit policy gate.

История: 2026-07-05 (#648) virtual TP/SL/MZE загейчены settings.live_exit_enabled —
«exit-паритет» с live BUY-only hold-to-resolution (research/virtual-stage-audit-2026-07-05).
2026-07-07 hold-to-resolution закреплён как самостоятельная политика (human decision по
exit-trajectories-audit-2026-07-06 + virtual-stage-audit): гейт отвязан от
live_exit_enabled и висит на собственном флаге settings.virtual_early_exit_enabled
(default False). Включение live-exit больше НЕ реанимирует virtual TP/SL молча.

Код TP/SL/MZE не удалён — только гейт на ПРИНЯТИЕ решения; флаг включается
осознанным human-решением, если политика когда-нибудь пересмотрится.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from app.core.config import Settings


def _make_settings(*, live_exit_enabled: bool = False, virtual_early_exit_enabled: bool = False) -> Settings:
    return Settings(
        database_url="postgresql://x:x@localhost/x",
        openai_api_key="sk-x",
        redis_url="redis://localhost",
        telegram_bot_token="1:x",
        admin_telegram_ids=[1],
        admin_api_key="x",
        polymarket_api_key="x",
        polymarket_api_secret="y",
        polymarket_api_passphrase="z",
        live_exit_enabled=live_exit_enabled,
        virtual_early_exit_enabled=virtual_early_exit_enabled,
    )


@pytest.mark.asyncio
async def test_virtual_tp_not_closed_when_live_exit_disabled(monkeypatch):
    """live_exit_enabled=False (default) → virtual TP-триггер НЕ закрывает позицию,
    держим до резолюции (как live)."""
    from app.jobs import position_manager as pm

    tp_row = {
        "id": "test-parity-tp",
        "detector": "STANDALONE_V2_LGB",
        "market_id": "0xparity",
        "side": "YES",
        "fill_price": 0.40,
        "stake": 100.0,
        "filled_at": datetime.now(UTC) - timedelta(minutes=15),
        "edge_pct": 10.0,
        "category": "crypto",
        "current_price": 0.60,  # +50% → TP trigger (STANDALONE tp=30)
        "market_resolved": False,
    }

    captured_updates: list = []

    async def mock_fetch(_sql, _arg):
        return [tp_row]

    async def mock_execute(*args):
        captured_updates.append(args)

    monkeypatch.setattr(pm, "fetch", mock_fetch)
    monkeypatch.setattr(pm, "execute", mock_execute)
    monkeypatch.setattr("app.core.config.get_settings", lambda: _make_settings(live_exit_enabled=False))

    result = await pm.run_position_manager()

    assert result["tp_closed"] == 0, "TP не должен закрывать virtual-позицию при live_exit_enabled=False"
    assert captured_updates == [], "никакого UPDATE — позиция держится до резолюции (паритет с live)"


@pytest.mark.asyncio
async def test_virtual_sl_not_closed_when_live_exit_disabled(monkeypatch):
    """live_exit_enabled=False → virtual SL-триггер тоже НЕ закрывает позицию."""
    from app.jobs import position_manager as pm

    sl_row = {
        "id": "test-parity-sl",
        "detector": "STANDALONE_V2_LGB",
        "market_id": "0xparity2",
        "side": "YES",
        "fill_price": 0.40,
        "stake": 100.0,
        "filled_at": datetime.now(UTC) - timedelta(minutes=90),  # SL-eligible age
        "edge_pct": 10.0,
        "category": "sports",
        "current_price": 0.31,  # -22.5% → SL trigger
        "market_resolved": False,
    }

    captured_updates: list = []

    async def mock_fetch(_sql, _arg):
        return [sl_row]

    async def mock_execute(*args):
        captured_updates.append(args)

    monkeypatch.setattr(pm, "fetch", mock_fetch)
    monkeypatch.setattr(pm, "execute", mock_execute)
    monkeypatch.setattr("app.core.config.get_settings", lambda: _make_settings(live_exit_enabled=False))

    result = await pm.run_position_manager()

    assert result["sl_closed"] == 0
    assert captured_updates == []


@pytest.mark.asyncio
async def test_virtual_mid_zone_exit_not_closed_when_live_exit_disabled(monkeypatch):
    """live_exit_enabled=False → mid_zone_exit тоже гейтится (весь early-exit скип)."""
    from app.jobs import position_manager as pm

    mze_row = {
        "id": "test-parity-mze",
        "detector": "D999_generic",
        "market_id": "0xparity3",
        "side": "YES",
        "fill_price": 0.50,
        "stake": 100.0,
        "filled_at": datetime.now(UTC) - timedelta(minutes=15),
        "edge_pct": 10.0,  # not high-edge — MZE eligible
        "category": "crypto",
        "current_price": 0.90,  # outside mid-zone [0.30, 0.85]
        "market_resolved": False,
    }

    captured_updates: list = []

    async def mock_fetch(_sql, _arg):
        return [mze_row]

    async def mock_execute(*args):
        captured_updates.append(args)

    monkeypatch.setattr(pm, "fetch", mock_fetch)
    monkeypatch.setattr(pm, "execute", mock_execute)
    monkeypatch.setattr("app.core.config.get_settings", lambda: _make_settings(live_exit_enabled=False))

    result = await pm.run_position_manager()

    assert result["mid_zone_exits"] == 0
    assert captured_updates == []


@pytest.mark.asyncio
async def test_virtual_tp_closes_when_virtual_early_exit_enabled(monkeypatch):
    """virtual_early_exit_enabled=True → virtual TP/SL/MZE-механика работает
    (код не удалён, включается собственным флагом, не live_exit_enabled)."""
    from app.jobs import position_manager as pm

    tp_row = {
        "id": "test-parity-tp-enabled",
        "detector": "STANDALONE_V2_LGB",
        "market_id": "0xparity4",
        "side": "YES",
        "fill_price": 0.40,
        "stake": 100.0,
        "filled_at": datetime.now(UTC) - timedelta(minutes=15),
        "edge_pct": 10.0,
        "category": "crypto",
        "current_price": 0.60,  # +50% → TP trigger
        "market_resolved": False,
    }

    captured_updates: list = []

    async def mock_fetch(_sql, _arg):
        return [tp_row]

    async def mock_execute(*args):
        captured_updates.append(args)

    monkeypatch.setattr(pm, "fetch", mock_fetch)
    monkeypatch.setattr(pm, "execute", mock_execute)
    monkeypatch.setattr(
        "app.core.config.get_settings", lambda: _make_settings(virtual_early_exit_enabled=True)
    )

    result = await pm.run_position_manager()

    assert result["tp_closed"] == 1, "при virtual_early_exit_enabled=True virtual TP должен сработать"
    assert captured_updates[0][1] == "take_profit"


@pytest.mark.asyncio
async def test_virtual_exits_stay_gated_when_live_exit_enabled(monkeypatch):
    """КЛЮЧЕВОЙ регрессионный тест решения 2026-07-07: включение live-exit НЕ должно
    молча реанимировать virtual TP/SL — virtual-гейт висит на собственном флаге
    virtual_early_exit_enabled, а не на live_exit_enabled."""
    from app.jobs import position_manager as pm

    tp_row = {
        "id": "test-decoupled-gate",
        "detector": "STANDALONE_V2_LGB",
        "market_id": "0xdecoupled",
        "side": "YES",
        "fill_price": 0.40,
        "stake": 100.0,
        "filled_at": datetime.now(UTC) - timedelta(minutes=15),
        "edge_pct": 10.0,
        "category": "crypto",
        "current_price": 0.60,  # +50% → TP-триггер, который обязан быть загейчен
        "market_resolved": False,
    }

    captured_updates: list = []

    async def mock_fetch(_sql, _arg):
        return [tp_row]

    async def mock_execute(*args):
        captured_updates.append(args)

    async def mock_live_contour():
        return {"checked": 0}

    monkeypatch.setattr(pm, "fetch", mock_fetch)
    monkeypatch.setattr(pm, "execute", mock_execute)
    # live-контур здесь не предмет теста — глушим, чтобы не тянуть его зависимости
    monkeypatch.setattr(pm, "run_live_position_manager", mock_live_contour)
    monkeypatch.setattr(
        "app.core.config.get_settings",
        lambda: _make_settings(live_exit_enabled=True, virtual_early_exit_enabled=False),
    )

    result = await pm.run_position_manager()

    assert result["tp_closed"] == 0, "live_exit_enabled=True не должен включать virtual TP/SL"
    assert captured_updates == [], "никакого virtual-UPDATE — hold-to-resolution закреплён отдельным флагом"


@pytest.mark.asyncio
async def test_d38_hold_to_resolution_unaffected_by_exit_parity_gate(monkeypatch):
    """D38 уже hold-to-resolution по своей логике (HOLD_TO_RESOLUTION_DETECTORS) —
    паритет-гейт не должен ничего сломать: с флагом False позиция всё равно held,
    counters ведут себя предсказуемо (mid_zone_exits=0, без крашей)."""
    from app.jobs import position_manager as pm

    d38_row = {
        "id": "test-d38-parity",
        "detector": "D38_resolution_lag",
        "market_id": "0xd38parity",
        "side": "NO",
        "fill_price": 0.05,
        "stake": 100.0,
        "filled_at": datetime.now(UTC) - timedelta(minutes=120),
        "edge_pct": 10.0,
        "category": "sports",
        "current_price": 0.95,  # extreme zone, hold-to-resolution bypasses MZE anyway
        "market_resolved": False,
    }

    captured_updates: list = []

    async def mock_fetch(_sql, _arg):
        return [d38_row]

    async def mock_execute(*args):
        captured_updates.append(args)

    monkeypatch.setattr(pm, "fetch", mock_fetch)
    monkeypatch.setattr(pm, "execute", mock_execute)
    monkeypatch.setattr("app.core.config.get_settings", lambda: _make_settings(live_exit_enabled=False))

    result = await pm.run_position_manager()

    assert result["mid_zone_exits"] == 0
    assert result["tp_closed"] == 0
    assert result["sl_closed"] == 0
    assert captured_updates == []


@pytest.mark.asyncio
async def test_d38_catastrophic_sl_also_gated_by_exit_parity(monkeypatch):
    """Circuit-breaker SL −80% у D38 при virtual_early_exit_enabled=False тоже
    держится до резолюции (counterfactual аудита 2026-07-05: hold для D38 не хуже
    SL). При True — срабатывает."""
    from app.jobs import position_manager as pm

    def _d38_catastrophic_row():
        return {
            "id": "test-d38-cat-sl",
            "detector": "D38_resolution_lag",
            "market_id": "0xd38catsl",
            "side": "NO",
            "fill_price": 0.50,
            "stake": 100.0,
            "filled_at": datetime.now(UTC) - timedelta(minutes=120),  # SL-eligible age
            "edge_pct": 10.0,
            "category": "sports",
            "current_price": 0.92,  # NO price 0.08 → pnl −84% ≤ −80% circuit-breaker
            "market_resolved": False,
        }

    captured_updates: list = []

    async def mock_fetch(_sql, _arg):
        return [_d38_catastrophic_row()]

    async def mock_execute(*args):
        captured_updates.append(args)

    monkeypatch.setattr(pm, "fetch", mock_fetch)
    monkeypatch.setattr(pm, "execute", mock_execute)

    monkeypatch.setattr("app.core.config.get_settings", lambda: _make_settings())
    result = await pm.run_position_manager()
    assert result["sl_closed"] == 0, "circuit-breaker SL D38 должен гейтиться (default hold-to-resolution)"
    assert captured_updates == []

    monkeypatch.setattr(
        "app.core.config.get_settings", lambda: _make_settings(virtual_early_exit_enabled=True)
    )
    result = await pm.run_position_manager()
    assert result["sl_closed"] == 1, "при virtual_early_exit_enabled=True circuit-breaker D38 работает"
    assert len(captured_updates) == 1


@pytest.mark.asyncio
async def test_exit_parity_skip_logged_once_per_run(monkeypatch):
    """Гейт логирует ОДИН раз за прогон (не per-position) когда exit-оценка скипнута
    из-за паритета с live — видимость поведения в логах без спама."""
    from app.jobs import position_manager as pm

    rows = [
        {
            "id": f"test-parity-log-{i}",
            "detector": "STANDALONE_V2_LGB",
            "market_id": f"0xparitylog{i}",
            "side": "YES",
            "fill_price": 0.40,
            "stake": 100.0,
            "filled_at": datetime.now(UTC) - timedelta(minutes=15),
            "edge_pct": 10.0,
            "category": "crypto",
            "current_price": 0.60,
            "market_resolved": False,
        }
        for i in range(3)
    ]

    async def mock_fetch(_sql, _arg):
        return rows

    async def mock_execute(*args):
        pass

    log_calls: list = []

    class _FakeLogger:
        def info(self, event, **kwargs):
            log_calls.append((event, kwargs))

        def warning(self, *a, **kw):
            pass

        def error(self, *a, **kw):
            pass

    monkeypatch.setattr(pm, "fetch", mock_fetch)
    monkeypatch.setattr(pm, "execute", mock_execute)
    monkeypatch.setattr(pm, "logger", _FakeLogger())
    monkeypatch.setattr("app.core.config.get_settings", lambda: _make_settings(live_exit_enabled=False))

    await pm.run_position_manager()

    skip_logs = [c for c in log_calls if c[0] == "position_manager_exit_parity_skip"]
    assert len(skip_logs) == 1, "гейт-лог должен появиться ровно один раз за прогон, не per-position"
