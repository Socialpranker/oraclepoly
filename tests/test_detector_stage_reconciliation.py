"""Unit tests для reconciliation backtest→shadow→virtual (H3).

Синтетика + temp-файлы, без БД. Проверяем pure-core divergence, sample-gate,
implied-haircut и frontmatter-reader.
"""

from __future__ import annotations

import pytest

from app.services.detector_stage_reconciliation import (
    MIN_N_SETTLED,
    DetectorSources,
    _parse_frontmatter,
    compute_divergence,
    read_backtest_projection,
    reconcile_detectors,
)


def _src(
    *,
    detector: str = "DX",
    backtest_roi: float | None = 50.0,
    haircut: float | None = 0.7,
    shadow_roi: float | None = 20.0,  # в ПРОЦЕНТАХ (как detector_signals.realized_roi_net)
    shadow_n: int = 100,
    virtual_sum: float | None = 1000.0,
    virtual_mean: float | None = 10.0,
    virtual_n: int = 50,
) -> DetectorSources:
    return DetectorSources(
        detector=detector,
        backtest_roi_net_pct=backtest_roi,
        backtest_haircut_factor=haircut,
        shadow_mean_roi_net=shadow_roi,
        shadow_n_signals=shadow_n,
        virtual_sum_pnl_usd=virtual_sum,
        virtual_mean_pnl_usd=virtual_mean,
        virtual_n_settled=virtual_n,
    )


# ─── sample-gate ─────────────────────────────────────────────────────────────


def test_insufficient_data_below_gate():
    """virtual_n_settled < 30 → insufficient_data, divergence не считается."""
    row = compute_divergence(_src(virtual_n=29))
    assert row.status == "insufficient_data"
    assert row.reason is not None and "29" in row.reason
    assert row.backtest_vs_shadow_divergence_pct is None
    assert row.implied_haircut_factor is None
    # virtual mean всё равно прокидывается (для контекста), но без divergence.
    assert row.virtual_mean_pnl_usd == 10.0


def test_gate_boundary_exactly_min_is_ok():
    """N ровно = MIN_N_SETTLED проходит gate (>=, не >)."""
    row = compute_divergence(_src(virtual_n=MIN_N_SETTLED))
    assert row.status == "ok"


# ─── divergence math ─────────────────────────────────────────────────────────


def test_divergence_backtest_optimistic():
    """backtest honest = 50 × 0.7 = 35%. shadow = 20% (уже в %, без конверсии).
    divergence = 35 - 20 = +15pp (backtest оптимистичнее — боль проекта)."""
    row = compute_divergence(_src(backtest_roi=50.0, haircut=0.7, shadow_roi=20.0))
    assert row.status == "ok"
    assert row.backtest_honest_roi_pct == pytest.approx(35.0)
    assert row.shadow_roi_pct == pytest.approx(20.0)
    assert row.backtest_vs_shadow_divergence_pct == pytest.approx(15.0)


def test_implied_haircut_aligns_backtest_to_shadow():
    """implied haircut = shadow_pct / backtest_gross = 20 / 50 = 0.4.
    Т.е. ручной 0.7 слишком оптимистичен, реальность требует 0.4."""
    row = compute_divergence(_src(backtest_roi=50.0, haircut=0.7, shadow_roi=20.0))
    assert row.implied_haircut_factor == pytest.approx(0.4)


def test_negative_divergence_when_shadow_beats_backtest():
    """shadow выше backtest-honest → divergence отрицательный (редкий случай)."""
    row = compute_divergence(_src(backtest_roi=10.0, haircut=0.7, shadow_roi=20.0))
    # honest = 7, shadow = 20 → divergence = 7 - 20 = -13.
    assert row.backtest_vs_shadow_divergence_pct == pytest.approx(-13.0)


def test_no_divergence_when_backtest_missing():
    """Нет backtest-дока → divergence/implied None, но статус ok (shadow есть)."""
    row = compute_divergence(_src(backtest_roi=None, haircut=None, shadow_roi=20.0))
    assert row.status == "ok"
    assert row.backtest_honest_roi_pct is None
    assert row.backtest_vs_shadow_divergence_pct is None
    assert row.shadow_roi_pct == pytest.approx(20.0)


def test_no_divergence_when_shadow_missing():
    """Нет shadow-данных → divergence None, virtual всё равно отражается."""
    row = compute_divergence(_src(shadow_roi=None, virtual_n=50))
    assert row.status == "ok"
    assert row.shadow_roi_pct is None
    assert row.backtest_vs_shadow_divergence_pct is None


def test_implied_haircut_guards_zero_gross():
    """backtest_gross=0 → деления на ноль нет, implied None."""
    row = compute_divergence(_src(backtest_roi=0.0, haircut=0.7, shadow_roi=20.0))
    assert row.implied_haircut_factor is None


# ─── reconcile_detectors (batch) ─────────────────────────────────────────────


def test_reconcile_detectors_mixed_batch():
    """Батч: один ok, один под gate → две строки с корректными статусами."""
    rows = reconcile_detectors([_src(detector="A", virtual_n=50), _src(detector="B", virtual_n=5)])
    by_det = {r.detector: r for r in rows}
    assert by_det["A"].status == "ok"
    assert by_det["B"].status == "insufficient_data"


# ─── frontmatter reader ──────────────────────────────────────────────────────


def test_parse_frontmatter_extracts_scalars():
    text = "---\ndetector: D3\nroi_net_pct: 47.3\nhonest_haircut_factor: 0.73\n---\n\n# Body\n"
    fm = _parse_frontmatter(text)
    assert fm["roi_net_pct"] == "47.3"
    assert fm["honest_haircut_factor"] == "0.73"


def test_parse_frontmatter_ignores_comments_and_no_block():
    assert _parse_frontmatter("no frontmatter here") == {}
    fm = _parse_frontmatter("---\n# comment\nkey: val\n---\n")
    assert fm == {"key": "val"}


def test_read_backtest_projection_from_file(tmp_path):
    """read_backtest_projection парсит roi_net_pct + haircut из {detector}_*.md."""
    docs = tmp_path / "backtests"
    docs.mkdir()
    (docs / "D3_cross_mkt_arb.md").write_text(
        "---\ndetector: D3_cross_mkt_arb\nroi_net_pct: 47.3\nhonest_haircut_factor: 0.73\n---\n# D3\n",
        encoding="utf-8",
    )
    roi, haircut = read_backtest_projection("D3_cross_mkt_arb", docs)
    assert roi == pytest.approx(47.3)
    assert haircut == pytest.approx(0.73)


def test_read_backtest_projection_missing_file(tmp_path):
    docs = tmp_path / "backtests"
    docs.mkdir()
    assert read_backtest_projection("D_nonexistent", docs) == (None, None)
