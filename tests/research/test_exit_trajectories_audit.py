"""Тесты чистых функций exit_trajectories_audit (без БД)."""

from datetime import UTC, datetime, timedelta

import pytest

from scripts.research.exit_trajectories_audit import (
    SignalTrajectory,
    bootstrap_mean_ci,
    compute_mae_mfe,
    simulate_tp_sl,
    staleness_stats,
    terminal_pnl_pct,
)


def _ts(minutes: int) -> datetime:
    return datetime(2026, 7, 1, tzinfo=UTC) + timedelta(minutes=minutes)


def make_traj(side: str, entry_yes: float, yes_path: list[float], outcome: str = "WIN") -> SignalTrajectory:
    return SignalTrajectory(
        signal_id="sig-1",
        detector="D38_resolution_lag",
        side=side,
        entry_yes=entry_yes,
        outcome=outcome,
        generated_at=_ts(0),
        resolved_at=_ts(60 * len(yes_path)),
        snapshots=[(_ts(5 * i), p) for i, p in enumerate(yes_path)],
    )


class TestComputeMaeMfe:
    def test_yes_side_drawdown_then_recovery(self):
        # entry 0.50, путь 0.40 (−20%) → 0.60 (+20%)
        traj = make_traj("YES", 0.50, [0.50, 0.40, 0.60])
        mae, mfe = compute_mae_mfe(traj)
        assert mae == pytest.approx(-20.0)
        assert mfe == pytest.approx(20.0)

    def test_no_side_inverts_price(self):
        # NO: entry_cost = 1-0.60 = 0.40; yes растёт 0.60→0.80 → cost 0.20 → −50%
        traj = make_traj("NO", 0.60, [0.60, 0.80])
        mae, mfe = compute_mae_mfe(traj)
        assert mae == pytest.approx(-50.0)
        assert mfe == pytest.approx(0.0)

    def test_monotone_winner_has_zero_mae(self):
        traj = make_traj("YES", 0.30, [0.30, 0.50, 0.90])
        mae, mfe = compute_mae_mfe(traj)
        assert mae == pytest.approx(0.0)
        assert mfe == pytest.approx(200.0)

    def test_empty_snapshots_returns_zeros(self):
        traj = make_traj("YES", 0.50, [])
        mae, mfe = compute_mae_mfe(traj)
        assert mae == pytest.approx(0.0)
        assert mfe == pytest.approx(0.0)


class TestTerminalPnl:
    def test_win_yes(self):
        # WIN на YES с entry 0.50 → (1-0.5)/0.5 = +100%
        assert terminal_pnl_pct(entry_yes=0.50, side="YES", outcome="WIN") == pytest.approx(100.0)

    def test_loss_any_side_is_minus_100(self):
        assert terminal_pnl_pct(entry_yes=0.50, side="YES", outcome="LOSS") == pytest.approx(-100.0)
        assert terminal_pnl_pct(entry_yes=0.30, side="NO", outcome="LOSS") == pytest.approx(-100.0)

    def test_win_no_uses_no_cost_basis(self):
        # NO с entry_yes 0.80 → cost 0.20 → WIN = (1-0.2)/0.2 = +400%
        assert terminal_pnl_pct(entry_yes=0.80, side="NO", outcome="WIN") == pytest.approx(400.0)


class TestSimulateTpSl:
    def test_no_sl_no_tp_is_hold_to_resolution(self):
        # (None, None) = hold: терминальный payoff минус haircut/2
        traj = make_traj("YES", 0.50, [0.50, 0.40, 0.60], outcome="WIN")
        res = simulate_tp_sl(traj, sl_pct=None, tp_pct=None, haircut_pct=3.0)
        assert res.exit_reason == "resolution"
        assert res.net_pnl_pct == pytest.approx(100.0 - 1.5)

    def test_sl_triggers_on_drawdown(self):
        # entry 0.50, дно 0.35 (−30%) — SL −20 срежет на первом снапшоте ≤ −20%
        traj = make_traj("YES", 0.50, [0.50, 0.35, 0.90], outcome="WIN")
        res = simulate_tp_sl(traj, sl_pct=-20.0, tp_pct=None, haircut_pct=0.0)
        assert res.exit_reason == "stop_loss"
        assert res.net_pnl_pct == pytest.approx(-30.0)  # exit по фактической цене снапшота

    def test_tp_triggers_before_sl_if_earlier_in_path(self):
        traj = make_traj("YES", 0.50, [0.50, 0.65, 0.30], outcome="LOSS")
        res = simulate_tp_sl(traj, sl_pct=-30.0, tp_pct=25.0, haircut_pct=0.0)
        assert res.exit_reason == "take_profit"
        assert res.net_pnl_pct == pytest.approx(30.0)  # 0.65 → +30%

    def test_early_exit_pays_full_haircut(self):
        traj = make_traj("YES", 0.50, [0.50, 0.65], outcome="WIN")
        res = simulate_tp_sl(traj, sl_pct=None, tp_pct=25.0, haircut_pct=4.0)
        assert res.net_pnl_pct == pytest.approx(30.0 - 4.0)

    def test_loss_without_sl_rides_to_minus_100(self):
        traj = make_traj("YES", 0.50, [0.50, 0.30, 0.10], outcome="LOSS")
        res = simulate_tp_sl(traj, sl_pct=None, tp_pct=None, haircut_pct=0.0)
        assert res.exit_reason == "resolution"
        assert res.net_pnl_pct == pytest.approx(-100.0)


class TestBootstrapMeanCi:
    def test_ci_contains_mean_and_is_deterministic(self):
        values = [1.0, 2.0, 3.0, 4.0, 5.0] * 20
        lo1, hi1 = bootstrap_mean_ci(values)
        lo2, hi2 = bootstrap_mean_ci(values)
        assert (lo1, hi1) == (lo2, hi2)  # seed фиксирован
        assert lo1 < 3.0 < hi1

    def test_empty_returns_nan(self):
        import math

        lo, hi = bootstrap_mean_ci([])
        assert math.isnan(lo) and math.isnan(hi)


class TestStalenessStats:
    def test_all_identical_prices_are_fully_stale(self):
        traj = make_traj("YES", 0.50, [0.50, 0.50, 0.50, 0.50])
        s = staleness_stats(traj)
        assert s["repeat_share"] == pytest.approx(1.0)
        assert s["max_plateau"] == 4

    def test_all_changing_prices_are_fresh(self):
        traj = make_traj("YES", 0.50, [0.50, 0.51, 0.52, 0.53])
        s = staleness_stats(traj)
        assert s["repeat_share"] == pytest.approx(0.0)
        assert s["max_plateau"] == 1

    def test_median_interval_minutes(self):
        traj = make_traj("YES", 0.50, [0.50, 0.51, 0.52])  # make_traj шаг 5 мин
        s = staleness_stats(traj)
        assert s["median_interval_min"] == pytest.approx(5.0)
