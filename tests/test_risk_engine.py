# tests/test_risk_engine.py
from datetime import datetime

from app.services.risk_engine import (
    OpenPosition,
    check_risk,
)


def _pos(
    market_id: str,
    event_id: str | None,
    category: str,
    stake: float,
    created: str = "2026-01-01T00:00:00+00:00",
    resolved: str | None = "2026-12-31T00:00:00+00:00",
) -> OpenPosition:
    return OpenPosition(
        market_id=market_id,
        event_id=event_id,
        category=category,
        stake=stake,
        created_at=datetime.fromisoformat(created),
        resolved_at=datetime.fromisoformat(resolved) if resolved else None,
    )


class TestEventExposure:
    def test_within_limit(self):
        """Event exposure < 10% → без урезания."""
        result = check_risk(
            proposed_stake=50.0,
            market_id="m1",
            event_id="e1",
            category="crypto",
            bank=1000.0,
            peak_bank=1000.0,
            open_positions=[_pos("m2", "e1", "crypto", 30.0)],
        )
        assert result.approved_stake == 50.0
        assert not result.is_blocked

    def test_exceeds_limit_trimmed(self):
        """Event exposure 8% + запрос 5% → одобрено только 2%."""
        result = check_risk(
            proposed_stake=50.0,
            market_id="m1",
            event_id="e1",
            category="crypto",
            bank=1000.0,
            peak_bank=1000.0,
            open_positions=[_pos("m2", "e1", "crypto", 80.0)],
        )
        # max = 100 (10%), used = 80, available = 20
        assert result.approved_stake == 20.0
        assert "event_cap" in result.reductions

    def test_none_event_id_skips_check(self):
        """event_id = None → проверка event exposure пропускается."""
        result = check_risk(
            proposed_stake=200.0,
            market_id="m1",
            event_id=None,
            category="crypto",
            bank=1000.0,
            peak_bank=1000.0,
            open_positions=[],
        )
        assert result.approved_stake > 0


class TestCategoryExposure:
    def test_exceeds_category_limit(self):
        """Category exposure > 30% → урезание."""
        positions = [_pos(f"m{i}", f"e{i}", "crypto", 60.0) for i in range(5)]
        result = check_risk(
            proposed_stake=50.0,
            market_id="m99",
            event_id="e99",
            category="crypto",
            bank=1000.0,
            peak_bank=1000.0,
            open_positions=positions,
        )
        assert result.approved_stake == 0.0
        assert result.is_blocked


class TestPortfolioHeat:
    def test_exceeds_heat(self):
        """Total exposure > 40% → урезание."""
        positions = [
            _pos("m1", "e1", "crypto", 200.0),
            _pos("m2", "e2", "politics", 200.0),
        ]
        result = check_risk(
            proposed_stake=50.0,
            market_id="m3",
            event_id="e3",
            category="sports",
            bank=1000.0,
            peak_bank=1000.0,
            open_positions=positions,
        )
        assert result.approved_stake == 0.0
        assert result.is_blocked


class TestDrawdownReduce:
    def test_drawdown_halves_stake(self):
        """Bank < peak × 0.8 → stake × 0.5."""
        result = check_risk(
            proposed_stake=50.0,
            market_id="m1",
            event_id="e1",
            category="crypto",
            bank=750.0,
            peak_bank=1000.0,
            open_positions=[],
        )
        assert result.approved_stake == 25.0
        assert "drawdown_reduce" in result.reductions

    def test_no_drawdown_no_reduction(self):
        """Bank = peak → без reduce."""
        result = check_risk(
            proposed_stake=50.0,
            market_id="m1",
            event_id="e1",
            category="crypto",
            bank=1000.0,
            peak_bank=1000.0,
            open_positions=[],
        )
        assert result.approved_stake == 50.0
        assert "drawdown_reduce" not in result.reductions


class TestCombined:
    def test_multiple_reductions_stack(self):
        """Event cap + drawdown → оба применяются."""
        result = check_risk(
            proposed_stake=100.0,
            market_id="m1",
            event_id="e1",
            category="crypto",
            bank=750.0,
            peak_bank=1000.0,
            open_positions=[_pos("m2", "e1", "crypto", 50.0)],
        )
        # Event: max=75 (10% of 750), used=50, available=25
        # Drawdown: 25 × 0.5 = 12.5
        assert result.approved_stake == 12.5
        assert "event_cap" in result.reductions
        assert "drawdown_reduce" in result.reductions

    def test_empty_positions_no_limits(self):
        """Пустой портфель → ставка без ограничений."""
        result = check_risk(
            proposed_stake=50.0,
            market_id="m1",
            event_id="e1",
            category="crypto",
            bank=1000.0,
            peak_bank=1000.0,
            open_positions=[],
        )
        assert result.approved_stake == 50.0
        assert not result.is_blocked


# --- Phase 1 Stage 3: per-bet cap + min stake floor ---


class TestPerBetCap:
    """5% cap per single trade. Документ §12: 'max 5% капитала на one trade'."""

    def test_per_bet_cap_reduces_above_5pct(self):
        """5% × $1000 = $50 max. Запрос $80 → reduced to $50."""
        result = check_risk(
            proposed_stake=80.0,
            market_id="m1",
            event_id="e1",
            category="crypto",
            bank=1000.0,
            peak_bank=1000.0,
            open_positions=[],
            max_per_bet=0.05,
        )
        assert result.approved_stake == 50.0
        assert "per_bet_cap" in result.reductions
        assert not result.is_blocked

    def test_per_bet_cap_noop_below_threshold(self):
        """$30 < 5% cap ($50) → unchanged, no reduction."""
        result = check_risk(
            proposed_stake=30.0,
            market_id="m1",
            event_id="e1",
            category="crypto",
            bank=1000.0,
            peak_bank=1000.0,
            open_positions=[],
            max_per_bet=0.05,
        )
        assert result.approved_stake == 30.0
        assert "per_bet_cap" not in result.reductions

    def test_per_bet_cap_disabled_via_zero(self):
        """max_per_bet=0 → cap отключён (backward compat для existing tests)."""
        # event_id=None → event_cap skip. cat/heat (30%/40%) ample для $200/1000.
        result = check_risk(
            proposed_stake=200.0,
            market_id="m1",
            event_id=None,
            category="crypto",
            bank=1000.0,
            peak_bank=1000.0,
            open_positions=[],
            max_per_bet=0.0,
            min_stake_usd=0.0,
        )
        assert result.approved_stake == 200.0
        assert "per_bet_cap" not in result.reductions


class TestMinStakeFloor:
    """Min viable stake $25. Документ §12: 'Min stake $25 на $10k bankroll'."""

    def test_min_stake_blocks_proposed_below(self):
        """$20 < $25 floor → block, не fill."""
        result = check_risk(
            proposed_stake=20.0,
            market_id="m1",
            event_id="e1",
            category="crypto",
            bank=1000.0,
            peak_bank=1000.0,
            open_positions=[],
            min_stake_usd=25.0,
        )
        assert result.is_blocked
        assert result.approved_stake == 0.0
        assert "below_min_stake" in result.reductions

    def test_min_stake_blocks_after_reduction(self):
        """$200 → caps свели к $5 (5% × $100 bank) → block, не fill."""
        result = check_risk(
            proposed_stake=200.0,
            market_id="m1",
            event_id="e1",
            category="crypto",
            bank=100.0,
            peak_bank=100.0,
            open_positions=[],
            max_per_bet=0.05,
            min_stake_usd=25.0,
        )
        assert result.is_blocked
        assert "per_bet_cap" in result.reductions
        assert "below_min_stake" in result.reductions
        assert result.approved_stake == 0.0

    def test_min_stake_allows_above_floor(self):
        """$30 > $25 floor → проходит."""
        result = check_risk(
            proposed_stake=30.0,
            market_id="m1",
            event_id="e1",
            category="crypto",
            bank=1000.0,
            peak_bank=1000.0,
            open_positions=[],
            min_stake_usd=25.0,
        )
        assert not result.is_blocked
        assert result.approved_stake == 30.0
        assert "below_min_stake" not in result.reductions

    def test_min_stake_disabled_via_zero(self):
        """min_stake_usd=0 → floor отключён (backward compat)."""
        result = check_risk(
            proposed_stake=5.0,
            market_id="m1",
            event_id="e1",
            category="crypto",
            bank=1000.0,
            peak_bank=1000.0,
            open_positions=[],
            min_stake_usd=0.0,
        )
        assert not result.is_blocked
        assert result.approved_stake == 5.0


class TestBackwardCompat:
    """Сигнатура check_risk остаётся совместимой со старыми вызовами.

    Defaults: max_per_bet=0.0 и min_stake_usd=0.0 (disabled). Активные
    значения передаются из _apply_risk_engine через Settings → opt-in.
    """

    def test_default_params_no_limits(self):
        """Без явных параметров → новые caps не активируются (backward compat)."""
        result = check_risk(
            proposed_stake=200.0,
            market_id="m1",
            event_id="e1",
            category="crypto",
            bank=1000.0,
            peak_bank=1000.0,
            open_positions=[],
        )
        # Без max_per_bet/min_stake → $200 проходит через event($100)/cat($300) caps,
        # event_cap трогает (50%>100=trim до 100). Это existing behaviour.
        assert "per_bet_cap" not in result.reductions
        assert "below_min_stake" not in result.reductions
