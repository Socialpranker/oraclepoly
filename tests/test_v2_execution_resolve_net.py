"""Тесты net PnL settlement в v2_execution_resolve.

Проверяет _compute_settlement_pnl: gross + net (после fee + slippage).
Замещает старый test_shadow_executor_pnl.py из v1 архитектуры.
"""

from __future__ import annotations

from unittest.mock import AsyncMock

import pytest

from app.jobs.v2_execution_resolve import _SETTLE_UPDATE_SQL, _compute_settlement_pnl


def test_settlement_win_low_entry():
    """NO @ 0.40 WIN на $10 stake: gross_pnl = 10/0.40 - 10 = $15.
    slippage_usd == 0 (fill_price уже отражает slippage, двойной счёт убран)."""
    res = _compute_settlement_pnl(stake=10.0, fill_price=0.40, outcome="WIN", category="crypto")
    assert res["pnl"] == pytest.approx(15.0, abs=0.01)
    assert res["trading_fee_usd"] >= 0
    assert res["slippage_usd"] == 0.0  # slippage больше НЕ вычитается отдельно
    assert res["net_pnl"] == pytest.approx(res["pnl"] - res["trading_fee_usd"], abs=0.01)
    assert res["net_pnl"] > 10.0


def test_settlement_loss_no_slippage_deduction():
    """LOSS на $10 stake: gross = −10, net = −10 − fee, slippage НЕ вычитается."""
    res = _compute_settlement_pnl(stake=10.0, fill_price=0.40, outcome="LOSS", category="sports")
    assert res["pnl"] == pytest.approx(-10.0, abs=0.01)
    assert res["slippage_usd"] == 0.0
    assert res["net_pnl"] == pytest.approx(-10.0 - res["trading_fee_usd"], abs=0.01)


def test_settlement_extreme_no_at_099_win_positive_net():
    """NO @ 0.99 WIN — gross +1.01%, fee малая (~0.0071), slippage не вычитается → net > 0.

    Fix 2026-07-16: раньше здесь ожидался net < 0 из-за двойного вычета slippage
    (stake × half_spread поверх уже реализованного в fill_price slippage). После
    фикса net = gross - fee ≈ 0.101 - 0.007 = 0.094 (положительный)."""
    res = _compute_settlement_pnl(stake=10.0, fill_price=0.99, outcome="WIN", category="crypto")
    # gross_pnl = 10/0.99 - 10 ≈ 0.101
    assert 0 < res["pnl"] < 0.15
    assert res["slippage_usd"] == 0.0
    assert res["net_pnl"] == pytest.approx(res["pnl"] - res["trading_fee_usd"], abs=0.0001)
    assert res["net_pnl"] > 0, f"NO @ 0.99 WIN should be net positive without double slippage, got {res['net_pnl']}"


def test_settlement_unknown_outcome_returns_zeros():
    """outcome != WIN/LOSS — возвращаем nuls (не упасть)."""
    res = _compute_settlement_pnl(stake=10.0, fill_price=0.40, outcome="EXPIRED", category="crypto")
    assert res == {"pnl": 0.0, "trading_fee_usd": 0.0, "slippage_usd": 0.0, "net_pnl": 0.0}


def test_settlement_live_default_as_penalty_zero_unchanged():
    """Live (as_penalty_usd не передан → default 0.0): поведение НЕ меняется.
    fill_price для live = реальный CLOB resp[price], slippage уже встроен → 0."""
    res = _compute_settlement_pnl(stake=10.0, fill_price=0.40, outcome="WIN", category="crypto")
    assert res["slippage_usd"] == 0.0
    assert res["net_pnl"] == pytest.approx(res["pnl"] - res["trading_fee_usd"], abs=0.01)


def test_settlement_virtual_as_penalty_deducted_from_net():
    """VIRTUAL: as_penalty_usd (adverse-selection $-стоимость, уже в долларах —
    shares×penalty_per_share) вычитается из net_pnl и попадает в slippage_usd для
    видимости. Баг 2026-07-21: penalty писался в БД при placement, но settle
    хардкодил slippage_usd=0.0 → virtual net_pnl завышался (D38-temp +30%)."""
    res = _compute_settlement_pnl(
        stake=10.0, fill_price=0.40, outcome="WIN", category="crypto", as_penalty_usd=0.75
    )
    assert res["slippage_usd"] == pytest.approx(0.75, abs=1e-6)  # видим в БД
    # net = gross - fee - as_penalty (а не gross - fee как раньше)
    assert res["net_pnl"] == pytest.approx(res["pnl"] - res["trading_fee_usd"] - 0.75, abs=0.01)


def test_settlement_virtual_as_penalty_on_loss():
    """LOSS с as_penalty: penalty усугубляет убыток (net = -stake - fee - penalty)."""
    res = _compute_settlement_pnl(
        stake=10.0, fill_price=0.40, outcome="LOSS", category="sports", as_penalty_usd=0.30
    )
    assert res["slippage_usd"] == pytest.approx(0.30, abs=1e-6)
    assert res["net_pnl"] == pytest.approx(-10.0 - res["trading_fee_usd"] - 0.30, abs=0.01)


def test_settlement_none_as_penalty_treated_as_zero():
    """as_penalty_usd=None (NULL из БД для старых/live-строк) → трактуется как 0.0."""
    res = _compute_settlement_pnl(
        stake=10.0, fill_price=0.40, outcome="WIN", category="crypto", as_penalty_usd=None
    )
    assert res["slippage_usd"] == 0.0
    assert res["net_pnl"] == pytest.approx(res["pnl"] - res["trading_fee_usd"], abs=0.01)


def test_settlement_zero_price_safe():
    """fill_price=0 — не падаем (используем eff=0.001)."""
    res = _compute_settlement_pnl(stake=10.0, fill_price=0.0, outcome="WIN", category="crypto")
    # 1/0.001 - 1 = 999 → gross_pnl = 9990
    assert res["pnl"] > 0  # просто не должно крашить


def test_settlement_no_category_uses_other():
    """category=None → fallback на 'other'."""
    res = _compute_settlement_pnl(stake=10.0, fill_price=0.40, outcome="WIN", category=None)
    assert res["pnl"] == pytest.approx(15.0, abs=0.01)
    assert res["net_pnl"] < res["pnl"]


def test_settlement_win_loss_fee_equal_one_leg():
    """Settlement = резолюция (winning shares выкупаются за $1 каждая, без trade
    на выходе). Обе ноги (WIN и LOSS) платят только entry fee на amount=stake.

    Fix 2026-05-18: раньше WIN считал 2×fee на gross_notional (stake/fill_price),
    что инфляровало fees в 5× против реальных Polymarket fees
    (см. docs.polymarket.com/trading/fees). Реальная формула:
      fee = amount × feeRate × p×(1-p) — на КАЖДОЙ trade.
      Резолюция = НЕ trade → fee = 0.
    """
    win = _compute_settlement_pnl(stake=10.0, fill_price=0.40, outcome="WIN", category="crypto")
    loss = _compute_settlement_pnl(stake=10.0, fill_price=0.40, outcome="LOSS", category="crypto")
    # Обе резолюции — одна entry trade, fee должна совпадать.
    assert win["trading_fee_usd"] == pytest.approx(loss["trading_fee_usd"], abs=1e-6)
    # Реальное значение для crypto: amount=10, p=0.40, feeRate=0.072.
    # fee = 10 × 0.072 × 0.40 × 0.60 = 0.1728
    assert win["trading_fee_usd"] == pytest.approx(0.1728, abs=1e-4)


def test_settlement_high_entry_gross_close_to_zero():
    """Edge case: NO @ 0.95 WIN: gross = 10/0.95 - 10 ≈ 0.526."""
    res = _compute_settlement_pnl(stake=10.0, fill_price=0.95, outcome="WIN", category="politics")
    assert res["pnl"] == pytest.approx(10 / 0.95 - 10, abs=0.01)


def test_settlement_returns_floats_not_ints():
    """Возвращаемые значения — float (round до 4 знаков)."""
    res = _compute_settlement_pnl(stake=10.0, fill_price=0.40, outcome="WIN", category="crypto")
    for k, v in res.items():
        assert isinstance(v, float), f"{k} should be float, got {type(v).__name__}"


# === Settle guard от гонки с exit-путём (audit second-tier) ===


def test_settle_update_sql_has_status_guard():
    """_SETTLE_UPDATE_SQL WHERE обязан перепроверить status IN ('filled',
    'virtual_filled') прямо перед UPDATE — иначе гонка resolve vs position_manager/
    live_exit (который мог успеть перевести строку в 'exiting'/другой статус между
    SELECT в _FETCH_SETTLEABLE_SQL и этим UPDATE) искажает PnL, кормящий promote-гейты."""
    assert "execution_orders.status IN ('filled', 'virtual_filled')" in _SETTLE_UPDATE_SQL
    # Guard должен идти в WHERE, а не где-то в SET/FROM.
    where_clause = _SETTLE_UPDATE_SQL.split("WHERE execution_orders.id = data.id", 1)[1]
    assert "status IN ('filled', 'virtual_filled')" in where_clause


@pytest.mark.asyncio
async def test_settle_filled_passes_guarded_sql_to_execute(monkeypatch):
    """_settle_filled вызывает execute с _SETTLE_UPDATE_SQL (содержащим guard),
    а не какой-то другой inline-строкой — регрессия на подмену запроса."""
    import app.jobs.v2_execution_resolve as resolve_mod

    row = {
        "id": "order-1",
        "execution_mode": "taker",
        "fill_price": 0.40,
        "stake": 10.0,
        "side": "YES",
        "market_id": "mkt-1",
        "outcome": "WIN",
        "winner": "yes",  # settle-гейт: live-outcome выводится из markets.winner
        "category": "crypto",
        "fee_schedule": None,
        "as_penalty_usd": None,  # live-строка: penalty не применяется
    }

    async def _fake_fetch(sql, *args):
        return [row]

    execute_mock = AsyncMock()
    monkeypatch.setattr(resolve_mod, "fetch", _fake_fetch)
    monkeypatch.setattr(resolve_mod, "execute", execute_mock)

    count = await resolve_mod._settle_filled()

    assert count == 1
    execute_mock.assert_awaited_once()
    called_sql = execute_mock.await_args.args[0]
    assert called_sql is _SETTLE_UPDATE_SQL
    assert "status IN ('filled', 'virtual_filled')" in called_sql
