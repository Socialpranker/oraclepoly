"""
Global Bank Manager — pure-math движок банкролл-менеджмента.

Без I/O, без async. State передаётся явно, engine stateless.
Потребители: CWI Paper, Whale Copy Simulation, Paper Portfolio.
"""

from __future__ import annotations

from dataclasses import dataclass, replace


@dataclass(frozen=True)
class BankConfig:
    """Параметры банк-менеджмента. Immutable."""

    initial_bankroll: float = 4000.0
    daily_limit: float = 4000.0
    stake_per_trade: float = 100.0
    max_trades_per_day: int = 50
    reinvest_rate: float = 0.10
    bankrupt_threshold: float = 10.0
    use_daily_bank: bool = True


@dataclass
class BankState:
    """Текущее состояние банкролла. Mutable через replace()."""

    bankroll: float
    total_withdrawn: float = 0.0
    total_reinvested: float = 0.0
    total_losses: float = 0.0
    today_spent: float = 0.0
    today_trades: int = 0
    is_bankrupt: bool = False


@dataclass(frozen=True)
class BankSettlement:
    """Результат settlement (закрытие позиции или конец дня)."""

    state: BankState
    reinvested: float = 0.0
    withdrawn: float = 0.0
    pnl: float = 0.0


class BankEngine:
    """
    Pure-math движок банкролл-менеджмента.

    Без I/O. State передаётся как аргумент, возвращается новый state.
    Потребитель решает как хранить state (DB / in-memory).
    """

    def __init__(self, config: BankConfig) -> None:
        self.config = config

    def can_open_trade(self, state: BankState) -> tuple[bool, str]:
        """Проверить все условия для открытия позиции. Returns (ok, reason)."""
        if state.is_bankrupt:
            return False, "bankrupt"
        if state.bankroll < self.config.stake_per_trade:
            return False, "insufficient_bankroll"
        if not self.config.use_daily_bank:
            return True, "ok"
        daily_limit = min(state.bankroll, self.config.daily_limit)
        if state.today_spent >= daily_limit:
            return False, "daily_limit"
        if self.config.max_trades_per_day > 0 and state.today_trades >= self.config.max_trades_per_day:
            return False, "max_trades"
        return True, "ok"

    def on_trade_opened(self, state: BankState, stake: float) -> BankState:
        """Обновить state после открытия позиции."""
        return replace(
            state,
            today_spent=round(state.today_spent + stake, 2),
            today_trades=state.today_trades + 1,
        )

    def on_trade_closed(self, state: BankState, net_pnl: float) -> BankSettlement:
        """
        Per-trade settlement: обновить банкролл после закрытия позиции.

        Profit > 0: reinvest_rate идёт в bankroll, остальное withdrawn.
        Loss: полностью вычитается из bankroll.
        """
        return self._apply_pnl(state, net_pnl)

    def settle_day(self, state: BankState, day_pnl: float) -> BankSettlement:
        """
        Daily settlement: обработать накопленный P&L за день.

        Используется в Whale Copy (simulation mode).
        Та же математика что on_trade_closed, но для дневного P&L.
        """
        return self._apply_pnl(state, day_pnl)

    def new_day(self, state: BankState) -> BankState:
        """Сбросить дневные счётчики. Вызывается на переходе дня."""
        bankrupt = state.bankroll < self.config.bankrupt_threshold
        return replace(
            state,
            today_spent=0.0,
            today_trades=0,
            is_bankrupt=bankrupt,
        )

    def _apply_pnl(self, state: BankState, pnl: float) -> BankSettlement:
        """Общая математика reinvest/withdraw для любого P&L."""
        if pnl > 0:
            reinvested = round(pnl * self.config.reinvest_rate, 4)
            withdrawn = round(pnl - reinvested, 4)
            new_state = replace(
                state,
                bankroll=round(state.bankroll + reinvested, 2),
                total_reinvested=round(state.total_reinvested + reinvested, 4),
                total_withdrawn=round(state.total_withdrawn + withdrawn, 4),
            )
            return BankSettlement(state=new_state, reinvested=reinvested, withdrawn=withdrawn, pnl=round(pnl, 4))

        if pnl < 0:
            loss = abs(pnl)
            new_bankroll = max(0.0, round(state.bankroll + pnl, 2))
            new_state = replace(
                state,
                bankroll=new_bankroll,
                total_losses=round(state.total_losses + loss, 4),
                is_bankrupt=new_bankroll < self.config.bankrupt_threshold,
            )
            return BankSettlement(state=new_state, reinvested=0.0, withdrawn=0.0, pnl=round(pnl, 4))

        return BankSettlement(state=state, pnl=0.0)
