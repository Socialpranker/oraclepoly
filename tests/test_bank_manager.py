"""Unit-тесты BankEngine — pure math, без I/O."""

import pytest

from app.core.bank_manager import BankConfig, BankEngine, BankState

# ─── Fixtures ─────────────────────────────────────────────────


@pytest.fixture
def default_config():
    return BankConfig(
        initial_bankroll=4000.0,
        daily_limit=4000.0,
        stake_per_trade=100.0,
        max_trades_per_day=50,
        reinvest_rate=0.10,
        bankrupt_threshold=10.0,
        use_daily_bank=True,
    )


@pytest.fixture
def engine(default_config):
    return BankEngine(default_config)


@pytest.fixture
def fresh_state():
    return BankState(bankroll=4000.0)


# ─── can_open_trade ───────────────────────────────────────────


class TestCanOpenTrade:
    def test_fresh_state_can_trade(self, engine, fresh_state):
        ok, reason = engine.can_open_trade(fresh_state)
        assert ok is True
        assert reason == "ok"

    def test_bankrupt_cannot_trade(self, engine):
        state = BankState(bankroll=0.0, is_bankrupt=True)
        ok, reason = engine.can_open_trade(state)
        assert ok is False
        assert reason == "bankrupt"

    def test_insufficient_bankroll(self, engine):
        state = BankState(bankroll=50.0)
        ok, reason = engine.can_open_trade(state)
        assert ok is False
        assert reason == "insufficient_bankroll"

    def test_daily_limit_reached(self, engine):
        state = BankState(bankroll=4000.0, today_spent=4000.0)
        ok, reason = engine.can_open_trade(state)
        assert ok is False
        assert reason == "daily_limit"

    def test_daily_limit_capped_by_bankroll(self, engine):
        state = BankState(bankroll=500.0, today_spent=500.0)
        ok, reason = engine.can_open_trade(state)
        assert ok is False
        assert reason == "daily_limit"

    def test_max_trades_reached(self, engine):
        state = BankState(bankroll=4000.0, today_trades=50)
        ok, reason = engine.can_open_trade(state)
        assert ok is False
        assert reason == "max_trades"

    def test_use_daily_bank_false_skips_daily_checks(self):
        config = BankConfig(use_daily_bank=False, stake_per_trade=100.0)
        engine = BankEngine(config)
        state = BankState(bankroll=200.0, today_spent=99999.0, today_trades=99999)
        ok, reason = engine.can_open_trade(state)
        assert ok is True

    def test_max_trades_zero_means_unlimited(self):
        config = BankConfig(max_trades_per_day=0, stake_per_trade=100.0)
        engine = BankEngine(config)
        state = BankState(bankroll=4000.0, today_trades=99999)
        ok, reason = engine.can_open_trade(state)
        assert ok is True


# ─── on_trade_opened ──────────────────────────────────────────


class TestOnTradeOpened:
    def test_updates_spent_and_count(self, engine, fresh_state):
        new_state = engine.on_trade_opened(fresh_state, 100.0)
        assert new_state.today_spent == 100.0
        assert new_state.today_trades == 1
        assert new_state.bankroll == 4000.0

    def test_accumulates(self, engine, fresh_state):
        s1 = engine.on_trade_opened(fresh_state, 100.0)
        s2 = engine.on_trade_opened(s1, 200.0)
        assert s2.today_spent == 300.0
        assert s2.today_trades == 2


# ─── on_trade_closed (per-trade settlement) ───────────────────


class TestOnTradeClosed:
    def test_profit_reinvest_withdraw(self, engine, fresh_state):
        settlement = engine.on_trade_closed(fresh_state, net_pnl=100.0)
        assert settlement.pnl == 100.0
        assert settlement.reinvested == 10.0
        assert settlement.withdrawn == 90.0
        assert settlement.state.bankroll == 4010.0
        assert settlement.state.total_reinvested == 10.0
        assert settlement.state.total_withdrawn == 90.0

    def test_loss_reduces_bankroll(self, engine, fresh_state):
        settlement = engine.on_trade_closed(fresh_state, net_pnl=-500.0)
        assert settlement.pnl == -500.0
        assert settlement.reinvested == 0.0
        assert settlement.withdrawn == 0.0
        assert settlement.state.bankroll == 3500.0
        assert settlement.state.total_losses == 500.0

    def test_loss_cannot_go_below_zero(self, engine):
        state = BankState(bankroll=100.0)
        settlement = engine.on_trade_closed(state, net_pnl=-200.0)
        assert settlement.state.bankroll == 0.0
        assert settlement.state.total_losses == 200.0

    def test_loss_triggers_bankrupt(self, engine):
        state = BankState(bankroll=15.0)
        settlement = engine.on_trade_closed(state, net_pnl=-10.0)
        assert settlement.state.bankroll == 5.0
        assert settlement.state.is_bankrupt is True

    def test_zero_pnl_no_change(self, engine, fresh_state):
        settlement = engine.on_trade_closed(fresh_state, net_pnl=0.0)
        assert settlement.state.bankroll == 4000.0
        assert settlement.reinvested == 0.0
        assert settlement.withdrawn == 0.0

    def test_custom_reinvest_rate(self):
        config = BankConfig(reinvest_rate=0.50)
        engine = BankEngine(config)
        state = BankState(bankroll=1000.0)
        settlement = engine.on_trade_closed(state, net_pnl=200.0)
        assert settlement.reinvested == 100.0
        assert settlement.withdrawn == 100.0
        assert settlement.state.bankroll == 1100.0

    def test_reinvest_rate_1_keeps_all(self):
        """reinvest_rate=1.0 = весь профит остаётся в банке (Paper Portfolio mode)."""
        config = BankConfig(reinvest_rate=1.0)
        engine = BankEngine(config)
        state = BankState(bankroll=1000.0)
        settlement = engine.on_trade_closed(state, net_pnl=200.0)
        assert settlement.reinvested == 200.0
        assert settlement.withdrawn == 0.0
        assert settlement.state.bankroll == 1200.0


# ─── settle_day (daily settlement for simulations) ────────────


class TestSettleDay:
    def test_profitable_day(self, engine, fresh_state):
        settlement = engine.settle_day(fresh_state, day_pnl=500.0)
        assert settlement.reinvested == 50.0
        assert settlement.withdrawn == 450.0
        assert settlement.state.bankroll == 4050.0

    def test_losing_day(self, engine, fresh_state):
        settlement = engine.settle_day(fresh_state, day_pnl=-300.0)
        assert settlement.reinvested == 0.0
        assert settlement.withdrawn == 0.0
        assert settlement.state.bankroll == 3700.0
        assert settlement.state.total_losses == 300.0


# ─── new_day ──────────────────────────────────────────────────


class TestNewDay:
    def test_resets_daily_counters(self, engine):
        state = BankState(bankroll=4000.0, today_spent=3000.0, today_trades=30)
        new_state = engine.new_day(state)
        assert new_state.today_spent == 0.0
        assert new_state.today_trades == 0
        assert new_state.bankroll == 4000.0

    def test_detects_bankrupt_on_new_day(self, engine):
        state = BankState(bankroll=5.0)
        new_state = engine.new_day(state)
        assert new_state.is_bankrupt is True

    def test_not_bankrupt_above_threshold(self, engine):
        state = BankState(bankroll=100.0)
        new_state = engine.new_day(state)
        assert new_state.is_bankrupt is False


# ─── Integration: multi-day simulation ────────────────────────


class TestMultiDaySimulation:
    def test_3_day_simulation(self):
        """Симуляция 3 дней: profit → loss → profit."""
        config = BankConfig(
            initial_bankroll=1000.0,
            daily_limit=1000.0,
            stake_per_trade=100.0,
            reinvest_rate=0.10,
            bankrupt_threshold=10.0,
        )
        engine = BankEngine(config)
        state = BankState(bankroll=1000.0)

        # Day 1: profit $200
        settlement = engine.settle_day(state, day_pnl=200.0)
        state = engine.new_day(settlement.state)
        assert state.bankroll == 1020.0
        assert settlement.withdrawn == 180.0

        # Day 2: loss $500
        settlement = engine.settle_day(state, day_pnl=-500.0)
        state = engine.new_day(settlement.state)
        assert state.bankroll == 520.0
        assert settlement.withdrawn == 0.0

        # Day 3: profit $100
        settlement = engine.settle_day(state, day_pnl=100.0)
        state = engine.new_day(settlement.state)
        assert state.bankroll == 530.0

    def test_bankrupt_stops_trading(self):
        config = BankConfig(
            initial_bankroll=100.0,
            bankrupt_threshold=10.0,
            stake_per_trade=50.0,
        )
        engine = BankEngine(config)
        state = BankState(bankroll=100.0)

        settlement = engine.on_trade_closed(state, net_pnl=-95.0)
        state = engine.new_day(settlement.state)
        assert state.is_bankrupt is True
        ok, reason = engine.can_open_trade(state)
        assert ok is False
        assert reason == "bankrupt"
