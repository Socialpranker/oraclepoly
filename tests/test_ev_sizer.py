"""Тесты EV sizer (Phase E.4)."""

from __future__ import annotations

import pytest

from app.calibration.ev_sizer import (
    FRACTIONAL_KELLY_COLD,
    FRACTIONAL_KELLY_FULL,
    SizingDecision,
    _kelly_fraction,
    size_from_calibrated_p,
)


def test_kelly_fraction_zero_when_no_edge():
    # p=0.5, payout=1 → fair coin, no edge → f*=0
    assert _kelly_fraction(0.5, 1.0) == pytest.approx(0.0)


def test_kelly_fraction_positive_when_edge():
    # p=0.6, payout=2 (NO @ 0.5 ставка) → f* = (0.6*2 - 0.4) / 2 = 0.4
    f = _kelly_fraction(0.6, 2.0)
    assert f == pytest.approx(0.4, rel=1e-3)


def test_kelly_fraction_negative_when_p_below_breakeven():
    # p=0.3, payout=2 → f* = (0.3*2 - 0.7) / 2 = -0.05 (negative)
    f = _kelly_fraction(0.3, 2.0)
    assert f < 0


def test_size_skip_when_p_invalid():
    d = size_from_calibrated_p(calibrated_p=0, entry_price=0.4, bankroll_usd=1000)
    assert d.stake_usd is None
    assert d.reason == "invalid_p"


def test_size_skip_when_entry_price_invalid():
    d = size_from_calibrated_p(calibrated_p=0.7, entry_price=0.0, bankroll_usd=1000)
    assert d.stake_usd is None
    assert d.reason == "invalid_entry_price"


def test_size_skip_when_no_bankroll():
    d = size_from_calibrated_p(calibrated_p=0.7, entry_price=0.4, bankroll_usd=0)
    assert d.stake_usd is None


def test_size_returns_stake_when_high_edge():
    """p=0.7, NO @ 0.4 → прибыль 1.5 на $1, EV = 0.7*1.5 - 0.3 = 0.75 (+75%).

    Было +145% — считали по валовому возврату 1/0.4=2.5 вместо прибыли 1.5.
    Исправлено 2026-08-10, см. research/favorite-buying-audit-2026-08-10.md §2.4.
    """
    d = size_from_calibrated_p(
        calibrated_p=0.7,
        entry_price=0.4,
        bankroll_usd=1000.0,
    )
    assert d.stake_usd is not None
    assert d.reason == "ok"
    assert d.ev_pct == pytest.approx(75.0, rel=1e-2)
    assert d.stake_usd > 0


def test_size_skip_when_ev_below_threshold():
    """p=0.42, NO @ 0.4 → EV = 0.42*2.5 - 0.58 = 0.47 (+47%) — выше 5%, не skip."""
    d = size_from_calibrated_p(
        calibrated_p=0.42,
        entry_price=0.4,
        bankroll_usd=1000.0,
        min_edge_pct=50.0,  # требуем 50%+ EV
    )
    assert d.stake_usd is None
    assert "ev_below_threshold" in d.reason


def test_stake_capped_by_bankroll_pct():
    """Очень высокий p → раз kelly большой, но stake ограничен max_stake_pct."""
    d = size_from_calibrated_p(
        calibrated_p=0.95,
        entry_price=0.10,  # NO @ 0.1 → payout 10x
        bankroll_usd=1000.0,
        max_stake_pct_of_bankroll=0.05,  # 5% bankroll cap
    )
    assert d.stake_usd is not None
    assert d.stake_usd <= 50.0  # 1000 * 5%


def test_fractional_kelly_full_vs_cold_difference():
    """Cold-start выдаёт меньшую ставку чем full Kelly при одинаковых p/price.

    Используем small bankroll + большой cap чтобы Kelly fraction решал, не cap.
    """
    full = size_from_calibrated_p(
        calibrated_p=0.55,
        entry_price=0.4,
        bankroll_usd=100.0,
        fractional_kelly=FRACTIONAL_KELLY_FULL,
        max_stake_pct_of_bankroll=1.0,  # cap отключён
    )
    cold = size_from_calibrated_p(
        calibrated_p=0.55,
        entry_price=0.4,
        bankroll_usd=100.0,
        fractional_kelly=FRACTIONAL_KELLY_COLD,
        max_stake_pct_of_bankroll=1.0,
    )
    assert full.stake_usd is not None
    assert cold.stake_usd is not None
    assert cold.stake_usd < full.stake_usd
    # cold = 0.20, full = 0.25 → ratio 0.80 (event-driven sizing)
    assert cold.stake_usd / full.stake_usd == pytest.approx(0.8, rel=1e-2)


def test_decision_is_frozen_dataclass():
    d = SizingDecision(stake_usd=10.0, reason="ok", ev_pct=50.0, p=0.7, entry_price=0.4)
    with pytest.raises(Exception):  # frozen → AttributeError or FrozenInstanceError
        d.stake_usd = 20.0  # type: ignore[misc]


def test_size_skip_when_p_at_bounds():
    """p=0 или p=1 → invalid (даже если бы Kelly был корректен)."""
    d0 = size_from_calibrated_p(calibrated_p=0.0, entry_price=0.4, bankroll_usd=1000)
    d1 = size_from_calibrated_p(calibrated_p=1.0, entry_price=0.4, bankroll_usd=1000)
    assert d0.stake_usd is None
    assert d1.stake_usd is None


# ─── Hard gate (entry_price ≥ 0.95) ──────────────────────────────────────────


def test_hard_gate_rejects_extreme_no_at_099():
    """NO @ 0.99: payout=1.01x, break-even WR ≈ 99% — математически нерентабельно."""
    d = size_from_calibrated_p(
        calibrated_p=0.999,
        entry_price=0.99,
        bankroll_usd=1000.0,
    )
    assert d.stake_usd is None
    assert "entry_price_ceiling" in d.reason


def test_hard_gate_rejects_at_exact_ceiling():
    """entry_price=0.95 ровно — должно отклоняться."""
    d = size_from_calibrated_p(
        calibrated_p=0.99,
        entry_price=0.95,
        bankroll_usd=1000.0,
    )
    assert d.stake_usd is None
    assert "entry_price_ceiling" in d.reason


def test_hard_gate_passes_just_below_ceiling():
    """entry_price=0.94 (< 0.95) — gate проходит, дальше уже EV check."""
    d = size_from_calibrated_p(
        calibrated_p=0.999,
        entry_price=0.94,
        bankroll_usd=1000.0,
    )
    # Не должен зарезаться hard gate (ceiling), но может ev check.
    assert "entry_price_ceiling" not in d.reason


def test_hard_gate_overrides_high_calibrated_p():
    """Даже p=0.9999 не спасает, если entry_price ≥ 0.95."""
    d = size_from_calibrated_p(
        calibrated_p=0.9999,
        entry_price=0.97,
        bankroll_usd=1000.0,
    )
    assert d.stake_usd is None
    assert "entry_price_ceiling" in d.reason


# ─── Net EV check (с category — fee + spread + buffer) ───────────────────────


def test_net_ev_check_skips_when_fee_eats_edge():
    """Marginal scenario: gross EV чуть выше min_edge, но fee+spread+buffer
    топят его → skip с net_ev_below_threshold."""
    # p=0.50, entry=0.45 → payout=2.222 → gross_ev = 0.50*2.222 - 0.50 = 0.611 (+61%)
    # Это всё ещё высоко. Берём более marginal: p=0.40, entry=0.45
    # 0.40 * 2.222 - 0.60 = 0.289 (+28.9%) — и здесь gross высок.
    # Чтобы получить gross ~ 5-7% — нужен почти fair: p ≈ entry_price
    # p=0.46, entry=0.45 → 0.46*2.222 - 0.54 = 0.482 (+48%) — всё ещё.
    # Низкий gross только когда p едва превышает entry. p=0.452, entry=0.45:
    # 0.452 * 2.222 - 0.548 = 0.456 (+4.6%) — ниже 5% min_edge → skip даже без fee
    # Берём p=0.46, entry=0.45 с min_edge_pct=45 → должен skip
    d = size_from_calibrated_p(
        calibrated_p=0.46,
        entry_price=0.45,
        bankroll_usd=1000.0,
        category="crypto",
        min_edge_pct=45.0,  # требуем 45% gross EV — gross 48% > 45%, но net < 47%
    )
    assert d.stake_usd is None
    assert "ev_below_threshold" in d.reason


def test_net_ev_check_passes_for_low_entry_high_p():
    """NO @ 0.4, p=0.7 → gross EV = 145%; fee+spread мизерны → проходит."""
    d = size_from_calibrated_p(
        calibrated_p=0.7,
        entry_price=0.4,
        bankroll_usd=1000.0,
        category="crypto",
    )
    assert d.stake_usd is not None
    assert d.reason == "ok"


def test_net_ev_includes_buffer_when_category_provided():
    """С category порог = min_edge_pct + EDGE_BUFFER_PCT: издержки топят слабый edge.

    p=0.42, entry=0.40 → прибыль 1.5 на $1, gross EV = 0.42*1.5 - 0.58 = +5.0%.
    Издержки 'other': fee 6.0% + half-spread 3.0% → net −4.0% < порога 7%.
    До фикса 2026-08-10 gross считался как +47% (по валовому возврату) и тест
    утверждал обратное — что такая ставка проходит.
    """
    d = size_from_calibrated_p(
        calibrated_p=0.42,
        entry_price=0.40,
        bankroll_usd=1000.0,
        category="other",
        min_edge_pct=5.0,
    )
    assert d.stake_usd is None
    assert "ev_below_threshold" in d.reason


def test_net_ev_path_legacy_compatibility_when_no_category():
    """Без category — legacy path: проверяем только gross EV. Проходит когда
    gross > min_edge_pct, даже если на проде net был бы отрицательным."""
    d = size_from_calibrated_p(
        calibrated_p=0.7,
        entry_price=0.4,
        bankroll_usd=1000.0,
        # category НЕ передан
    )
    assert d.stake_usd is not None
    assert d.reason == "ok"


# ─── Экономический инвариант EV (аудит 2026-08-10) ───────────────────────────
#
# EV на $1 stake при hold-to-resolution = p × (выигрыш) − (1−p) × (проигрыш).
# Выигрыш = payout = 1/entry − 1 (ПРИБЫЛЬ на $1, не валовой возврат).
# Проигрыш = 1 (теряем весь stake).
# Отсюда: EV = p × (1/entry − 1) − (1−p).
#
# До фикса код использовал payout = 1/entry (валовой возврат), что завышало EV
# ровно на p×100 пп: сайзер ставил максимальный кап на ставки с истинным
# EV −5.6% и −33%. См. research/favorite-buying-audit-2026-08-10.md §2.4.


def _true_ev_pct(p: float, entry: float) -> float:
    """Эталон EV, выведенный из определения, а не из кода сайзера."""
    return (p * (1.0 / entry - 1.0) - (1.0 - p)) * 100.0


@pytest.mark.parametrize(
    ("p", "entry"),
    [
        (0.91, 0.895),  # D38 факт: WR ≈ цене входа
        (0.95, 0.895),
        (0.70, 0.400),  # низкий вход, реальный edge
        (0.50, 0.480),
        (0.60, 0.900),  # заведомо убыточная
    ],
)
def test_ev_pct_matches_true_hold_to_resolution_ev(p: float, entry: float):
    """ev_pct обязан равняться экономическому EV, а не валовому возврату."""
    d = size_from_calibrated_p(calibrated_p=p, entry_price=entry, bankroll_usd=1000.0)
    assert d.ev_pct == pytest.approx(_true_ev_pct(p, entry), abs=0.01)


@pytest.mark.parametrize(
    ("p", "entry"),
    [
        (0.85, 0.90),  # истинный EV −5.6%
        (0.60, 0.90),  # истинный EV −33.3%
        (0.40, 0.50),  # истинный EV −20%
    ],
)
def test_rejects_negative_ev_bets(p: float, entry: float):
    """Ставка с отрицательным истинным EV не должна получать stake."""
    assert _true_ev_pct(p, entry) < 0, "тест-кейс должен быть убыточным по построению"
    d = size_from_calibrated_p(
        calibrated_p=p,
        entry_price=entry,
        bankroll_usd=1000.0,
        category="sports",
    )
    assert d.stake_usd is None, f"сайзер поставил {d.stake_usd} на EV={_true_ev_pct(p, entry):.2f}%"


def test_fair_coin_at_fair_price_has_zero_ev():
    """Монетка по честной цене: gross EV ровно 0, ставки быть не должно.

    Без category возвращается gross EV (legacy-путь), с category — net после
    издержек, поэтому эталон проверяем на gross-ветке.
    """
    d = size_from_calibrated_p(calibrated_p=0.5, entry_price=0.5, bankroll_usd=1000.0)
    assert d.ev_pct == pytest.approx(0.0, abs=0.01)
    assert d.stake_usd is None
