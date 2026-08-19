"""Tests для effective_fill_counts — переклассификация would-have-filled."""
from __future__ import annotations


def test_drift_reclassified_as_filled():
    """would-have-filled добавляется в числитель."""
    from app.services.detector_metrics import effective_fill_counts
    # D3 кейс: filled=27, drift=38, placed=65
    n_filled_eff, n_placed_eff = effective_fill_counts(n_filled=27, n_would_have_filled=38, n_placed=65)
    assert n_filled_eff == 65  # 27 + 38
    assert n_placed_eff == 65


def test_no_drift_unchanged():
    """Без would-have-filled — числа не меняются."""
    from app.services.detector_metrics import effective_fill_counts
    n_filled_eff, n_placed_eff = effective_fill_counts(n_filled=10, n_would_have_filled=0, n_placed=20)
    assert n_filled_eff == 10
    assert n_placed_eff == 20


def test_capped_at_n_placed():
    """n_filled_eff не может превышать n_placed."""
    from app.services.detector_metrics import effective_fill_counts
    # Edge: сумма больше placed (не должно происходить, но защита)
    n_filled_eff, n_placed_eff = effective_fill_counts(n_filled=50, n_would_have_filled=30, n_placed=60)
    assert n_filled_eff == 60  # capped, не 80
    assert n_placed_eff == 60


def test_zero_placed():
    """n_placed=0 — возвращает нули без деления."""
    from app.services.detector_metrics import effective_fill_counts
    n_filled_eff, n_placed_eff = effective_fill_counts(n_filled=0, n_would_have_filled=0, n_placed=0)
    assert n_filled_eff == 0
    assert n_placed_eff == 0


def test_no_book_not_counted():
    """no_book НЕ входит в would_have_filled — детектор с неликвидностью штрафуется.

    Гипотетический: filled=10, would_have_filled=5 (drift), placed=100 (85 no_book).
    Эффективный fill = 15/100 = 15% — низкий, что правильно (рынки неликвидны).
    """
    from app.services.detector_metrics import effective_fill_counts
    n_filled_eff, n_placed_eff = effective_fill_counts(n_filled=10, n_would_have_filled=5, n_placed=100)
    assert n_filled_eff == 15
    assert n_placed_eff == 100
    # 15/100 = 15% — gate с порогом 50% это отклонит (правильно)
