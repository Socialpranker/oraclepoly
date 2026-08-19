"""Unit tests для honest promotion gates v3 (2026-05-27).

5 gates: sample ≥40, net PnL > 0, catastrophic < 3%, BE WR payout-aware, Sortino > 1.0.
Pass rule: ≥4/5 → READY.

См. research/shadow-gates-methodology-2026-05/2026-05-27_validation.md
"""

from __future__ import annotations

import pytest

from app.services.detector_metrics import (
    _CATASTROPHIC_TAIL_THRESHOLD_PCT,
    _GATES_V2_PASS_THRESHOLD,
    _SAMPLE_SIZE_THRESHOLD,
    _SORTINO_THRESHOLD,
    compute_promotion_gates,
    evaluate_demotion_candidates,
)

# ─── Sanity: thresholds reflect research decisions ───────────────────────────


def test_thresholds_match_research_decisions():
    """v3 thresholds должны соответствовать research/shadow-gates-methodology-2026-05/."""
    assert _SAMPLE_SIZE_THRESHOLD == 40, "Wilson valid N≥40 для prediction markets"
    assert _CATASTROPHIC_TAIL_THRESHOLD_PCT == 3.0, "v3 tightened 5% → 3%"
    assert _SORTINO_THRESHOLD == 1.0, "Sortino > 1.0 industry standard"
    assert _GATES_V2_PASS_THRESHOLD == 4, "≥4/5 gates → ready"


# ─── compute_promotion_gates: v3 gate logic ──────────────────────────────────


def _full_shadow(
    terminal=50,
    wins=30,
    sum_roi_net=2.0,
    mean_roi_net=0.04,
    sum_neg_sq=0.5,
    median_entry=0.50,
    catastrophic=0,
):
    """Helper: shadow data c всеми v3 fields."""
    return {
        "distinct_events_7d": terminal,
        "distinct_markets_7d": terminal,
        "wins_7d": wins,
        "terminal_7d": terminal,
        "resolved_7d": terminal,
        "total_7d": terminal + 10,
        "sum_roi_net_7d": sum_roi_net,
        "mean_roi_net_7d": mean_roi_net,
        "sum_neg_sq_roi_7d": sum_neg_sq,
        "median_entry_price_7d": median_entry,
        "catastrophic_losses_7d": catastrophic,
    }


def test_all_5_gates_pass_returns_ready_v2():
    """5/5 gates passed → ready_v2 = True, gates_v2_passed = 5.

    Sortino consistency: mean_roi=0.04 + sum_neg_sq=0.02, terminal=50 →
    downside_vol = √(0.02/50) = 0.02 → Sortino = 0.04/0.02 = 2.0 > 1.0.
    (Прежнее sum_neg_sq=0.5 давало Sortino 0.4 — internally inconsistent
    для «all-pass» сценария: 4% средний доход не может сосуществовать
    с такой большой downside semivariance.)

    G4 consistency (2026-08-10): было wins=30 (WR 60%) при entry 0.50, то есть
    BE 52% — Wilson lower bound на n=50 даёт 46.2%, edge статистически не
    доказан. Для сценария «все гейты проходят» нужен реальный запас:
    wins=36 (WR 72%) → lower bound 58.3% > 52%.
    """
    sd = _full_shadow(terminal=50, wins=36, sum_roi_net=2.0, mean_roi_net=0.04, sum_neg_sq=0.02)
    out = compute_promotion_gates(shadow_data=sd, backtest=None, fingerprint=None)

    assert out["ready_v2"] is True
    assert out["gates_v2_passed"] == 5
    assert out["gates_v2_total"] == 5
    for gate_name, gate in out["gates_v2"].items():
        assert gate["passed"] is True, f"gate {gate_name} expected pass: {gate}"

    # net_pnl.value_pct отображается человеку на promote-дашборде и уже в %
    # (sum_roi_net = SUM(realized_roi_net), сам в %) — НЕ домножать на 100.
    # Ловит регрессию units-бага: при ×100 здесь было бы 200.0 вместо 2.0.
    assert out["gates_v2"]["net_pnl"]["value_pct"] == pytest.approx(2.0)
    assert out["shadow_7d"]["sum_roi_net_pct"] == pytest.approx(2.0)
    assert out["shadow_7d"]["mean_roi_net_pct"] == pytest.approx(0.04)


def test_small_sample_blocks():
    """terminal < 40 → G1/G2/G3 fail, even с positive PnL."""
    sd = _full_shadow(terminal=20, wins=12, sum_roi_net=2.0, mean_roi_net=0.10, sum_neg_sq=0.2)
    out = compute_promotion_gates(shadow_data=sd, backtest=None, fingerprint=None)

    assert out["gates_v2"]["sample"]["passed"] is False
    assert out["gates_v2"]["net_pnl"]["passed"] is False  # depends on g1_v2
    assert out["gates_v2"]["tail_risk"]["passed"] is False
    # G4 BE WR может пройти (60% > 33%), G5 sortino тоже
    # Итого до 2/5 → blocked
    assert out["gates_v2_passed"] <= 2
    assert out["ready_v2"] is False


def test_catastrophic_tail_blocks_g3():
    """5% catastrophic > 3% threshold → G3 fail."""
    sd = _full_shadow(terminal=100, catastrophic=5)  # 5%
    out = compute_promotion_gates(shadow_data=sd, backtest=None, fingerprint=None)

    assert out["gates_v2"]["tail_risk"]["passed"] is False
    assert out["gates_v2"]["tail_risk"]["value_pct"] == 5.0


def test_catastrophic_under_3pct_passes_g3():
    """2% catastrophic < 3% threshold → G3 pass."""
    sd = _full_shadow(terminal=100, catastrophic=2)  # 2%
    out = compute_promotion_gates(shadow_data=sd, backtest=None, fingerprint=None)

    assert out["gates_v2"]["tail_risk"]["passed"] is True


def test_be_wr_is_entry_plus_costs_not_inverted_payout():
    """BE при hold-to-resolution = entry + издержки, а не 1/(1+1/entry).

    Регрессия 2026-08-10: старая формула для entry 0.10 давала BE 9.1% (ниже
    самой цены входа!), для entry 0.895 — 47.2% вместо 89.5%. Ошибка росла
    с ценой входа и пропускала покупателей фаворитов.
    """
    sd = _full_shadow(terminal=100, wins=12, median_entry=0.10)
    out = compute_promotion_gates(shadow_data=sd, backtest=None, fingerprint=None)

    g4 = out["gates_v2"]["break_even_wr"]
    assert g4["source"] == "entry_plus_costs"
    # 0.10 → 10% + 2.0пп издержек = 12.0%, а НЕ 9.1% старой формулы.
    assert g4["threshold_pct"] == pytest.approx(12.0, abs=0.1)
    # WR 12% ровно на breakeven, Wilson lower bound заведомо ниже → fail.
    assert g4["passed"] is False, f"WR на границе BE не должен проходить: {g4}"


def test_be_wr_scales_with_entry_price_for_favorites():
    """Покупка фаворита @0.90: BE 92%, наблюдённый WR 91% → fail.

    Старая формула дала бы BE 47.4% и пропустила бы этот случай (D38-класс).
    """
    sd = _full_shadow(terminal=1000, wins=910, median_entry=0.90)
    out = compute_promotion_gates(shadow_data=sd, backtest=None, fingerprint=None)

    g4 = out["gates_v2"]["break_even_wr"]
    assert g4["threshold_pct"] == pytest.approx(92.0, abs=0.1)
    assert g4["observed_wr_pct"] == pytest.approx(91.0, abs=0.1)
    assert g4["passed"] is False


def test_g4_uses_wilson_lower_bound_not_point_estimate():
    """Решает Wilson lower bound: маленькая выборка не проходит на одном везении."""
    # 9/10 = WR 90% при BE 52% — точечная оценка прошла бы с запасом,
    # но при n=10 нижняя граница ≈ 60% — всё ещё проходит.
    big = _full_shadow(terminal=1000, wins=900, median_entry=0.50)
    small = _full_shadow(terminal=40, wins=36, median_entry=0.50)
    out_big = compute_promotion_gates(shadow_data=big, backtest=None, fingerprint=None)
    out_small = compute_promotion_gates(shadow_data=small, backtest=None, fingerprint=None)

    g4_big = out_big["gates_v2"]["break_even_wr"]
    g4_small = out_small["gates_v2"]["break_even_wr"]
    # Одинаковый наблюдённый WR 90%, но разная доказанная нижняя граница.
    assert g4_big["observed_wr_pct"] == pytest.approx(90.0, abs=0.1)
    assert g4_small["observed_wr_pct"] == pytest.approx(90.0, abs=0.1)
    assert g4_big["value"] > g4_small["value"], "больше выборка → уже интервал"
    assert g4_big["value"] < 90.0, "lower bound строго ниже точечной оценки"


def test_virtual_fingerprint_used_only_when_tp_sl_exist():
    """Fingerprint берётся, только если TP/SL реально были.

    С 2026-06-29 ранние выходы отключены: n_tp=n_sl=0 → break_even_wr_pct
    из fingerprint не значит ничего, нужен fallback на entry+издержки.
    """
    sd = _full_shadow(terminal=100, wins=45, median_entry=0.10)
    fp_alive = {
        "n_settled": 50,
        "n_tp": 30,
        "n_sl": 20,
        "break_even_wr_pct": 33.3,
    }
    fp_dead = {
        "n_settled": 50,
        "n_tp": 0,
        "n_sl": 0,
        "break_even_wr_pct": 33.3,  # мусор: посчитан без единого TP/SL
    }
    out_alive = compute_promotion_gates(shadow_data=sd, backtest=None, fingerprint=fp_alive)
    out_dead = compute_promotion_gates(shadow_data=sd, backtest=None, fingerprint=fp_dead)

    assert out_alive["gates_v2"]["break_even_wr"]["source"] == "virtual_fingerprint"
    assert out_alive["gates_v2"]["break_even_wr"]["threshold_pct"] == 33.3
    # Мёртвый fingerprint игнорируется → падаем в честный fallback.
    assert out_dead["gates_v2"]["break_even_wr"]["source"] == "entry_plus_costs"
    assert out_dead["gates_v2"]["break_even_wr"]["threshold_pct"] == pytest.approx(12.0, abs=0.1)


def test_sortino_gate_blocks_negative_mean():
    """Negative mean_roi_net → sortino < 0 → G5 fail."""
    sd = _full_shadow(terminal=100, mean_roi_net=-0.05, sum_neg_sq=5.0)
    out = compute_promotion_gates(shadow_data=sd, backtest=None, fingerprint=None)

    g5 = out["gates_v2"]["sortino"]
    assert g5["passed"] is False
    assert isinstance(g5["value"], (int, float))
    assert g5["value"] < 0


def test_sortino_gate_passes_positive_mean():
    """Positive mean / небольшая downside vol → sortino > 1.0 → G5 pass."""
    # mean = 0.04, sum_neg_sq = 0.5, terminal = 100
    # downside_vol = sqrt(0.5 / 100) = 0.0707
    # sortino = 0.04 / 0.0707 ≈ 0.566 → fail
    # для pass нужен меньший downside или больший mean
    sd = _full_shadow(terminal=100, mean_roi_net=0.10, sum_neg_sq=0.5)
    out = compute_promotion_gates(shadow_data=sd, backtest=None, fingerprint=None)

    g5 = out["gates_v2"]["sortino"]
    # sortino = 0.10 / sqrt(0.5/100) = 0.10 / 0.0707 ≈ 1.414 → pass
    assert g5["passed"] is True
    assert g5["value"] > 1.0


def test_legacy_gates_still_present():
    """Backward-compat: legacy gates (3 total) сохранены в payload."""
    sd = _full_shadow()
    out = compute_promotion_gates(shadow_data=sd, backtest=None, fingerprint=None)

    assert "gates" in out
    assert out["gates_total"] == 3
    assert {"n_events", "wr_match", "verdict"} == set(out["gates"].keys())


def test_empty_shadow_returns_safe_blocked():
    """None shadow_data → ничего не падает, gates_v2_passed = 0."""
    out = compute_promotion_gates(shadow_data=None, backtest=None, fingerprint=None)

    assert out["ready_v2"] is False
    assert out["gates_v2_passed"] == 0


# ─── evaluate_demotion_candidates ────────────────────────────────────────────


def test_demotion_batch_flags_only_failing_detector():
    """В батче из двух virtual-детекторов флагуется только тот, чья
    signal-level экономика провалена; здоровый — нет."""
    virtual_metrics = {
        "STANDALONE_V2_LGB": {"n_settled": 77, "pnl_usd": 7580.0},
        "D3_cross_mkt_arb": {"n_settled": 60, "pnl_usd": 826.0},
    }
    shadow_signals = {
        "STANDALONE_V2_LGB": {
            "terminal_7d": 60,
            "wins_7d": 12,
            "sum_roi_net_7d": -300.0,
            "mean_roi_net_7d": -5.0,
            "sum_neg_sq_roi_7d": 5000.0,
            "median_entry_price_7d": 0.6,
        },
        "D3_cross_mkt_arb": {
            "terminal_7d": 60,
            "wins_7d": 40,
            "sum_roi_net_7d": 245.0,
            "mean_roi_net_7d": 4.08,
            "sum_neg_sq_roi_7d": 100.0,
            "median_entry_price_7d": 0.4,
        },
    }
    candidates = evaluate_demotion_candidates(virtual_metrics, shadow_signals)

    assert [c["detector"] for c in candidates] == ["STANDALONE_V2_LGB"]
    assert "signal economics failed" in candidates[0]["reason"]


def test_demotion_skips_low_sample():
    """terminal < 40 → не trigger даже при провальной экономике (шум/малая выборка)."""
    virtual_metrics = {"D44_late_convergence_carry": {"n_settled": 35, "pnl_usd": -100.0}}
    shadow_signals = {
        "D44_late_convergence_carry": {
            "terminal_7d": 20,  # < 40
            "wins_7d": 2,
            "sum_roi_net_7d": -500.0,
            "mean_roi_net_7d": -25.0,
            "sum_neg_sq_roi_7d": 9000.0,
            "median_entry_price_7d": 0.5,
        },
    }
    candidates = evaluate_demotion_candidates(virtual_metrics, shadow_signals)

    assert candidates == []


def test_demotion_skips_detector_without_shadow_data():
    """Детектор в virtual_metrics, но без свежих shadow-сигналов → skip (не падаем)."""
    virtual_metrics = {"D26_fade_whale_extreme": {"n_settled": 50, "pnl_usd": -700.0}}
    shadow_signals: dict = {}  # нет 7d сигналов
    candidates = evaluate_demotion_candidates(virtual_metrics, shadow_signals)

    assert candidates == []


def test_demotion_uses_sample_threshold():
    """Demote v4 использует тот же sample floor, что promote (N≥40 terminal)."""
    assert _SAMPLE_SIZE_THRESHOLD == 40


# ─── Signal-level demote economics (v4 2026-05-29) ───────────────────────────
# Багфикс: order-level pnl_usd надут paper EARLY_EXIT (STANDALONE +$12K paper,
# resolved −$717). Resolved-order метрики нежизнеспособны (STANDALONE 4 WIN/LOSS
# ордера, D3/D14/D26/D44 = 0). Решение: demote судит по signal-level
# realized_roi_net (та же экономика, что promote-гейт), симметрично promote.
# См. memory [[shadow-roi-virtual-pnl]] + [[live-signal-trajectories-decay]].


def test_demote_candidate_when_signal_economics_fail():
    """Virtual детектор с N≥40 terminal сигналов и проваленной экономикой
    (sum_roi_net ≤ 0) → demote candidate. STANDALONE-кейс: paper PnL плюсовой,
    но честная signal-level экономика отрицательная."""
    virtual_metrics = {
        "STANDALONE_V2_LGB": {"n_settled": 77, "pnl_usd": 7580.0},  # paper плюс
    }
    shadow_signals = {
        "STANDALONE_V2_LGB": {
            "terminal_7d": 60,
            "wins_7d": 12,
            "sum_roi_net_7d": -300.0,  # honest economics отрицательна
            "mean_roi_net_7d": -5.0,
            "sum_neg_sq_roi_7d": 5000.0,
            "median_entry_price_7d": 0.6,
        },
    }
    candidates = evaluate_demotion_candidates(virtual_metrics, shadow_signals)

    assert len(candidates) == 1
    assert candidates[0]["detector"] == "STANDALONE_V2_LGB"


def test_no_demote_when_economics_healthy():
    """Virtual детектор с положительной экономикой (sum_roi_net>0 И Sortino>1.0)
    на N≥40 → НЕ demote candidate. D3-кейс."""
    virtual_metrics = {"D3_cross_mkt_arb": {"n_settled": 60, "pnl_usd": 826.0}}
    shadow_signals = {
        "D3_cross_mkt_arb": {
            "terminal_7d": 60,
            "wins_7d": 40,
            "sum_roi_net_7d": 245.0,  # положительна
            "mean_roi_net_7d": 4.08,
            "sum_neg_sq_roi_7d": 100.0,  # малый downside → Sortino высокий
            "median_entry_price_7d": 0.4,
        },
    }
    candidates = evaluate_demotion_candidates(virtual_metrics, shadow_signals)

    assert candidates == []
