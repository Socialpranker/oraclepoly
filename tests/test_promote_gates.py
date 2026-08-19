"""Mandatory promote gates: N≥30, Wilson lower bounds, adversarial mandatory.

См. docs/superpowers/plans/2026-05-honest-baseline/03-mandatory-gates.md.
"""

from __future__ import annotations

from app.services.promote_gates import evaluate_promote_gates


def _gate(**over):
    """Helper: evaluate_promote_gates с дефолтами проходящего virtual→live кейса.

    v4 2026-05-29: PnL-гейт судит по signal-level sum_roi_net + Sortino (не order pnl_usd).
    Дефолты — здоровый боец: sum_roi_net>0, Sortino>1.0, signal WR высокий.
    """
    kwargs = dict(
        n_terminal=50,
        n_placed=50,
        n_filled=45,
        wins=40,             # WR 80% → Wilson lower well above 50% floor
        losses=10,
        sum_roi_net=120.0,   # signal-level realized_roi_net сумма (%), >0
        mean_roi_net=2.4,    # средний ROI% per signal
        sum_neg_sq_roi=80.0,  # downside semivariance numerator
        median_entry_price=None,  # None → flat 50% WR floor (payout-aware если задан)
        days_virtual=20,
        adversarial_pass=True,
        execution_enabled=True,
        min_n=30,
    )
    kwargs.update(over)
    return evaluate_promote_gates(**kwargs)


# ── v4: PnL-гейт на signal-level экономике (sum_roi_net + Sortino) ────────────


def test_g1_fails_when_signal_econ_negative_despite_order_pnl():
    """STANDALONE-кейс: order pnl_usd был бы +$12K (paper), но signal-level
    экономика отрицательна (sum_roi_net<0) → G1 fail → не READY."""
    v = _gate(sum_roi_net=-30.0, mean_roi_net=-0.6, sum_neg_sq_roi=400.0)
    assert v["gates"]["pnl_positive"]["passed"] is False
    assert v["decision"] != "READY"


def test_g1_passes_when_sum_roi_positive_and_sortino_above_1():
    """sum_roi_net>0 И Sortino>1.0 → G1 pass."""
    # mean=2.4, sum_neg_sq=80, terminal=50 → downside=sqrt(80/50)=1.265 → Sortino=1.9>1
    v = _gate(sum_roi_net=120.0, mean_roi_net=2.4, sum_neg_sq_roi=80.0)
    assert v["gates"]["pnl_positive"]["passed"] is True


def test_g1_fails_when_sortino_below_1_even_if_sum_positive():
    """sum_roi_net>0, но Sortino<1.0 (большая downside vol) → G1 fail."""
    # mean=0.5, sum_neg_sq=5000, terminal=50 → downside=10 → Sortino=0.05<1
    v = _gate(sum_roi_net=25.0, mean_roi_net=0.5, sum_neg_sq_roi=5000.0)
    assert v["gates"]["pnl_positive"]["passed"] is False


def test_g1_sortino_degenerate_no_downside_positive_mean_passes():
    """sum_neg_sq=0 (нет лоссов) + mean>0 → Sortino=inf → G1 pass."""
    v = _gate(sum_roi_net=50.0, mean_roi_net=1.0, sum_neg_sq_roi=0.0)
    assert v["gates"]["pnl_positive"]["passed"] is True


def test_g1_no_fade_low_winrate_but_profitable_passes_payout_aware():
    """NO-fade @ entry 0.10 (payout 10×, BE WR ~9%): прибыльный детектор с
    низким hold-to-resolution WR (~20%) НЕ должен false-блокироваться flat 50%.

    Регрессия из code-review #532: G1 переключил WR-вход на signal-level
    (структурно низкий у fade-детекторов), но flat 50% floor делал их
    математически непромоутаемыми. Должен быть payout-aware (как G4 shadow→virtual)."""
    # 60 wins / 300 terminal = 20% WR; sum_roi_net>0, Sortino>1; median_entry=0.10.
    v = _gate(
        n_terminal=300, wins=60, losses=240,
        sum_roi_net=250.0, mean_roi_net=0.83, sum_neg_sq_roi=0.0,
        median_entry_price=0.10,
    )
    g1 = v["gates"]["pnl_positive"]
    assert g1["passed"] is True, f"NO-fade 20% WR > BE ~9% должен проходить: {g1}"


def test_g1_mid_zone_below_payout_be_wr_blocks():
    """Mid-zone @ entry 0.50 (payout 2×, BE WR ~33%): WR 20% < BE 33% → G1 fail."""
    v = _gate(
        n_terminal=300, wins=60, losses=240,
        sum_roi_net=250.0, mean_roi_net=0.83, sum_neg_sq_roi=0.0,
        median_entry_price=0.50,
    )
    assert v["gates"]["pnl_positive"]["passed"] is False


# ── Decision: INSUFFICIENT_DATA при N < min_n ────────────────────────────────


class TestNMinGate:
    def test_n_terminal_below_30_returns_insufficient_data(self):
        verdict = evaluate_promote_gates(
            n_terminal=12,
            n_placed=20,
            n_filled=15,
            wins=8,
            losses=4,
            sum_roi_net=50.0, mean_roi_net=1.0, sum_neg_sq_roi=0.0,
            days_virtual=14,
            adversarial_pass=True,
            execution_enabled=False,
            min_n=30,
        )
        assert verdict["decision"] == "INSUFFICIENT_DATA"
        assert verdict["gates"]["n_min"]["passed"] is False
        assert verdict["gates"]["n_min"]["value"] == 12

    def test_n_terminal_30_passes_n_gate(self):
        verdict = evaluate_promote_gates(
            n_terminal=30,
            n_placed=50,
            n_filled=40,
            wins=20,
            losses=10,
            sum_roi_net=100.0, mean_roi_net=1.0, sum_neg_sq_roi=0.0,
            days_virtual=14,
            adversarial_pass=True,
            execution_enabled=False,
            min_n=30,
        )
        assert verdict["gates"]["n_min"]["passed"] is True

    def test_n_below_overrides_other_failures(self):
        """N<30 → decision=INSUFFICIENT_DATA даже если другие gates fail.

        Логика: мало данных = непонятно, ждём, не блокируем по другим критериям."""
        verdict = evaluate_promote_gates(
            n_terminal=10,
            n_placed=10,
            n_filled=2,
            wins=1,
            losses=1,
            sum_roi_net=-5.0, mean_roi_net=1.0, sum_neg_sq_roi=0.0,
            days_virtual=5,
            adversarial_pass=False,
            execution_enabled=False,
            min_n=30,
        )
        assert verdict["decision"] == "INSUFFICIENT_DATA"


# ── Wilson fill rate gate ────────────────────────────────────────────────────


class TestWilsonFillRate:
    def test_4_out_of_4_wilson_lower_passes_50pct(self):
        """4/4 → Wilson lower ≈ 51% > 50% → passes G2."""
        verdict = evaluate_promote_gates(
            n_terminal=4,
            n_placed=4,
            n_filled=4,
            wins=4,
            losses=0,
            sum_roi_net=10.0, mean_roi_net=1.0, sum_neg_sq_roi=0.0,
            days_virtual=14,
            adversarial_pass=True,
            execution_enabled=False,
            min_n=4,
        )
        assert verdict["gates"]["fill_rate"]["passed"] is True

    def test_30_filled_60pct_blocks(self):
        """18/30 → Wilson lower ≈ 42% < 50% → FAILS G2."""
        verdict = evaluate_promote_gates(
            n_terminal=30,
            n_placed=30,
            n_filled=18,
            wins=10,
            losses=5,
            sum_roi_net=20.0, mean_roi_net=1.0, sum_neg_sq_roi=0.0,
            days_virtual=14,
            adversarial_pass=True,
            execution_enabled=False,
            min_n=30,
        )
        assert verdict["gates"]["fill_rate"]["passed"] is False
        # Wilson lower in percentage
        assert verdict["gates"]["fill_rate"]["wilson_lower_pct"] < 50.0

    def test_30_filled_85pct_passes(self):
        """25/30 → Wilson lower ≈ 66% > 50% → PASSES G2."""
        verdict = evaluate_promote_gates(
            n_terminal=30,
            n_placed=30,
            n_filled=25,
            wins=15,
            losses=8,
            sum_roi_net=40.0, mean_roi_net=1.0, sum_neg_sq_roi=0.0,
            days_virtual=14,
            adversarial_pass=True,
            execution_enabled=False,
            min_n=30,
        )
        assert verdict["gates"]["fill_rate"]["passed"] is True

    def test_n_placed_zero_passes(self):
        """N_placed=0 → нет данных о fill rate, ставим passed=True (decoupled от N gate).

        Если N_terminal достаточный но n_placed=0 (странно но возможно для shadow
        only detector), не блокируем по fill rate."""
        verdict = evaluate_promote_gates(
            n_terminal=30,
            n_placed=0,
            n_filled=0,
            wins=15,
            losses=15,
            sum_roi_net=10.0, mean_roi_net=1.0, sum_neg_sq_roi=0.0,
            days_virtual=14,
            adversarial_pass=True,
            execution_enabled=False,
            min_n=30,
        )
        # No placed → no data — passed=True by default (нет evidence against)
        assert verdict["gates"]["fill_rate"]["passed"] is True


# ── Win rate (PnL gate) ──────────────────────────────────────────────────────


class TestPnlAndWinrateGate:
    def test_positive_pnl_with_strong_winrate_passes(self):
        """30 wins / 35 terminal → wr=85.7%, Wilson lower ≈ 70% > 50%."""
        verdict = evaluate_promote_gates(
            n_terminal=35,
            n_placed=35,
            n_filled=35,
            wins=30,
            losses=5,
            sum_roi_net=100.0, mean_roi_net=1.0, sum_neg_sq_roi=0.0,
            days_virtual=14,
            adversarial_pass=True,
            execution_enabled=False,
            min_n=30,
        )
        assert verdict["gates"]["pnl_positive"]["passed"] is True

    def test_negative_pnl_blocks_even_with_high_winrate(self):
        """PnL < 0 → blocks независимо от win rate (выигрышные ставки малые, проигрышные большие)."""
        verdict = evaluate_promote_gates(
            n_terminal=30,
            n_placed=30,
            n_filled=30,
            wins=25,
            losses=5,
            sum_roi_net=-50.0, mean_roi_net=1.0, sum_neg_sq_roi=0.0,
            days_virtual=14,
            adversarial_pass=True,
            execution_enabled=False,
            min_n=30,
        )
        assert verdict["gates"]["pnl_positive"]["passed"] is False

    def test_marginal_winrate_blocks(self):
        """16 wins / 30 terminal → wr=53%, Wilson lower ≈ 35% < 50%."""
        verdict = evaluate_promote_gates(
            n_terminal=30,
            n_placed=30,
            n_filled=30,
            wins=16,
            losses=14,
            sum_roi_net=10.0, mean_roi_net=1.0, sum_neg_sq_roi=0.0,
            days_virtual=14,
            adversarial_pass=True,
            execution_enabled=False,
            min_n=30,
        )
        assert verdict["gates"]["pnl_positive"]["passed"] is False


# ── Adversarial mirror (CRITICAL) ────────────────────────────────────────────


class TestAdversarialMandatory:
    def test_adversarial_none_returns_manual_override(self):
        """adversarial_pass is None → MANUAL_OVERRIDE_NEEDED (не auto-pass).

        Это критическая разница с v1 поведением, где None → True."""
        verdict = evaluate_promote_gates(
            n_terminal=50,
            n_placed=50,
            n_filled=40,
            wins=25,
            losses=15,
            sum_roi_net=100.0, mean_roi_net=1.0, sum_neg_sq_roi=0.0,
            days_virtual=14,
            adversarial_pass=None,
            execution_enabled=True,
            min_n=30,
        )
        assert verdict["decision"] == "MANUAL_OVERRIDE_NEEDED"
        assert verdict["gates"]["adversarial_mirror"]["passed"] is False
        assert verdict["gates"]["adversarial_mirror"]["value"] == "not_evaluated"

    def test_adversarial_false_blocks(self):
        """adversarial_pass=False → BLOCKED."""
        verdict = evaluate_promote_gates(
            n_terminal=50,
            n_placed=50,
            n_filled=40,
            wins=25,
            losses=15,
            sum_roi_net=100.0, mean_roi_net=1.0, sum_neg_sq_roi=0.0,
            days_virtual=14,
            adversarial_pass=False,
            execution_enabled=True,
            min_n=30,
        )
        assert verdict["decision"] == "BLOCKED"
        assert verdict["gates"]["adversarial_mirror"]["passed"] is False
        assert verdict["gates"]["adversarial_mirror"]["value"] == "fail"

    def test_adversarial_true_passes(self):
        """adversarial_pass=True + все остальные gates → READY."""
        verdict = evaluate_promote_gates(
            n_terminal=50,
            n_placed=50,
            n_filled=40,
            wins=30,
            losses=10,
            sum_roi_net=200.0, mean_roi_net=1.0, sum_neg_sq_roi=0.0,
            days_virtual=14,
            adversarial_pass=True,
            execution_enabled=True,
            min_n=30,
        )
        assert verdict["decision"] == "READY"
        assert verdict["gates"]["adversarial_mirror"]["passed"] is True


# ── Other gates ──────────────────────────────────────────────────────────────


class TestDaysAndExecGates:
    def test_days_below_14_blocks(self):
        verdict = evaluate_promote_gates(
            n_terminal=50,
            n_placed=50,
            n_filled=40,
            wins=30,
            losses=10,
            sum_roi_net=200.0, mean_roi_net=1.0, sum_neg_sq_roi=0.0,
            days_virtual=5,
            adversarial_pass=True,
            execution_enabled=True,
            min_n=30,
        )
        assert verdict["gates"]["days_accumulated"]["passed"] is False
        assert verdict["decision"] == "BLOCKED"

    def test_execution_enabled_false_blocks_final(self):
        """С execution_enabled=False decision = BLOCKED даже если всё остальное OK.

        Это намеренно: G5 — кill switch уровня env, manual decision by the owner."""
        verdict = evaluate_promote_gates(
            n_terminal=50,
            n_placed=50,
            n_filled=40,
            wins=30,
            losses=10,
            sum_roi_net=200.0, mean_roi_net=1.0, sum_neg_sq_roi=0.0,
            days_virtual=20,
            adversarial_pass=True,
            execution_enabled=False,
            min_n=30,
        )
        assert verdict["gates"]["execution_enabled"]["passed"] is False
        assert verdict["decision"] == "BLOCKED"


# ── Overall decision logic ──────────────────────────────────────────────────


class TestOverallDecision:
    def test_all_pass_returns_ready(self):
        verdict = evaluate_promote_gates(
            n_terminal=50,
            n_placed=50,
            n_filled=45,
            wins=30,
            losses=15,
            sum_roi_net=200.0, mean_roi_net=1.0, sum_neg_sq_roi=0.0,
            days_virtual=20,
            adversarial_pass=True,
            execution_enabled=True,
            min_n=30,
        )
        assert verdict["decision"] == "READY"
        # Все gates passed
        for name, gate in verdict["gates"].items():
            assert gate["passed"] is True, f"gate {name} not passed"

    def test_decision_priority_order(self):
        """Priority: INSUFFICIENT_DATA > MANUAL_OVERRIDE_NEEDED > BLOCKED > READY."""
        # N=10, adversarial=None, execution=False — все три условия активны
        verdict = evaluate_promote_gates(
            n_terminal=10,
            n_placed=10,
            n_filled=8,
            wins=4,
            losses=2,
            sum_roi_net=-5.0, mean_roi_net=1.0, sum_neg_sq_roi=0.0,
            days_virtual=5,
            adversarial_pass=None,
            execution_enabled=False,
            min_n=30,
        )
        # N gate fail имеет priority — decision=INSUFFICIENT_DATA, не MANUAL_OVERRIDE
        assert verdict["decision"] == "INSUFFICIENT_DATA"

    def test_n_passes_then_manual_override_priority_over_blocked(self):
        """N OK, adversarial=None, exec=False → MANUAL_OVERRIDE (не BLOCKED)."""
        verdict = evaluate_promote_gates(
            n_terminal=50,
            n_placed=50,
            n_filled=40,
            wins=30,
            losses=10,
            sum_roi_net=200.0, mean_roi_net=1.0, sum_neg_sq_roi=0.0,
            days_virtual=20,
            adversarial_pass=None,
            execution_enabled=False,
            min_n=30,
        )
        # adversarial=None → MANUAL_OVERRIDE дальше всего
        # exec=False — также не passed но второстепенно
        assert verdict["decision"] == "MANUAL_OVERRIDE_NEEDED"

    def test_default_min_n_from_settings(self):
        """Если min_n=None → читаем из settings.promote_gate_min_n_terminal (default 30)."""
        verdict = evaluate_promote_gates(
            n_terminal=25,
            n_placed=25,
            n_filled=20,
            wins=15,
            losses=5,
            sum_roi_net=50.0, mean_roi_net=1.0, sum_neg_sq_roi=0.0,
            days_virtual=14,
            adversarial_pass=True,
            execution_enabled=True,
        )
        # N=25 < default 30 → INSUFFICIENT_DATA
        assert verdict["decision"] == "INSUFFICIENT_DATA"


# ── Gates dict structure ─────────────────────────────────────────────────────


class TestGatesDictStructure:
    def test_all_six_gates_present(self):
        verdict = evaluate_promote_gates(
            n_terminal=50,
            n_placed=50,
            n_filled=45,
            wins=30,
            losses=15,
            sum_roi_net=200.0, mean_roi_net=1.0, sum_neg_sq_roi=0.0,
            days_virtual=20,
            adversarial_pass=True,
            execution_enabled=True,
            min_n=30,
        )
        expected_gates = {
            "n_min",
            "pnl_positive",
            "fill_rate",
            "adversarial_mirror",
            "days_accumulated",
            "execution_enabled",
        }
        assert set(verdict["gates"].keys()) == expected_gates

    def test_each_gate_has_passed_and_value(self):
        verdict = evaluate_promote_gates(
            n_terminal=50,
            n_placed=50,
            n_filled=45,
            wins=30,
            losses=15,
            sum_roi_net=200.0, mean_roi_net=1.0, sum_neg_sq_roi=0.0,
            days_virtual=20,
            adversarial_pass=True,
            execution_enabled=True,
            min_n=30,
        )
        for name, gate in verdict["gates"].items():
            assert "passed" in gate, f"gate {name} missing 'passed'"
            assert "value" in gate, f"gate {name} missing 'value'"

    def test_wilson_metrics_in_pnl_and_fill_gates(self):
        verdict = evaluate_promote_gates(
            n_terminal=30,
            n_placed=30,
            n_filled=25,
            wins=20,
            losses=10,
            sum_roi_net=50.0, mean_roi_net=1.0, sum_neg_sq_roi=0.0,
            days_virtual=14,
            adversarial_pass=True,
            execution_enabled=True,
            min_n=30,
        )
        assert "wilson_lower_pct" in verdict["gates"]["fill_rate"]
        assert "wilson_lower_winrate_pct" in verdict["gates"]["pnl_positive"]


class TestWilsonFillRateWouldHaveFilled:
    """G2 fill rate с переклассификацией would-have-filled (drift rejections)."""

    def _base_kwargs(self):
        """Минимальные kwargs чтобы дойти до проверки fill_rate gate."""
        return dict(
            n_terminal=50,
            wins=30,
            losses=20,
            sum_roi_net=10.0,
            mean_roi_net=0.5,
            sum_neg_sq_roi=2.0,
            median_entry_price=0.5,
            days_virtual=20,
            adversarial_pass=True,
            execution_enabled=True,
        )

    def test_drift_reclassified_passes_fill_gate(self):
        """D3-кейс: filled=27, drift=38, placed=65 → эффективный 65/65=100% → pass."""
        from app.services.promote_gates import evaluate_promote_gates
        verdict = evaluate_promote_gates(
            n_placed=65,
            n_filled=27,
            n_would_have_filled=38,
            **self._base_kwargs(),
        )
        assert verdict["gates"]["fill_rate"]["passed"] is True
        assert verdict["gates"]["fill_rate"]["value"] == 100.0

    def test_no_book_still_blocks_fill_gate(self):
        """Детектор с реальной неликвидностью: filled=10, drift=5, placed=100 (85 no_book).

        Эффективный fill = 15/100 = 15% → Wilson lower < 50% → fill gate FAILS.
        """
        from app.services.promote_gates import evaluate_promote_gates
        verdict = evaluate_promote_gates(
            n_placed=100,
            n_filled=10,
            n_would_have_filled=5,
            **self._base_kwargs(),
        )
        assert verdict["gates"]["fill_rate"]["passed"] is False

    def test_default_would_have_filled_zero(self):
        """Без n_would_have_filled (default 0) — поведение как раньше."""
        from app.services.promote_gates import evaluate_promote_gates
        verdict = evaluate_promote_gates(
            n_placed=30,
            n_filled=18,
            **self._base_kwargs(),
        )
        # 18/30 = 60%, Wilson lower ~42% < 50% → fail (как было до фикса)
        assert verdict["gates"]["fill_rate"]["passed"] is False
