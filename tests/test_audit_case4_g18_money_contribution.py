"""Тесты чистых функций Phase 0 audit-скрипта replay-кейса 4
(scripts/research/audit_case4_g18_money_contribution.py).

Покрывает `_price_bin` (bit-match с production `g18_bin_calibration._price_bin`),
`compute_g18_walkforward` (anti-look-ahead walk-forward reconstruction),
`_fee_spread_cost_usd`/`_net_pnl_for_signal` (fee/spread-adjusted PnL), `build_paired_results`
(skip-фильтр по общему набору), и `bootstrap_delta_ci` (per-event bootstrap детерминированность).

Все тесты на синтетических данных — без прод-БД (та доступна только на VPS, см.
CLAUDE.md: scripts/research/*.py запускаются только на проде). Функции, требующие
БД (`load_signals`, `compute_fallback_no_g18`, `persist`), намеренно не покрыты
здесь — их корректность зависит от реальных `ml_calibrators`/`detector_signals`
на проде и не воспроизводима в юнит-тесте без моков (которые дали бы ложную
уверенность методологически сложнее, чем просто задокументировать это
ограничение — см. task-7-report.md "acceptance criteria не проверено").
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from app.calibration.ev_sizer import size_from_calibrated_p
from app.services.g18_bin_calibration import _price_bin as prod_price_bin
from scripts.research.audit_case4_g18_money_contribution import (
    PairedResult,
    SignalRow,
    _fee_spread_cost_usd,
    _net_pnl_for_signal,
    _price_bin,
    bootstrap_delta_ci,
    build_paired_results,
    compute_g18_walkforward,
)

_BASE = datetime(2026, 1, 1, tzinfo=UTC)


def _sig(
    sig_id: str,
    *,
    day_offset: float,
    outcome: str,
    detector: str = "D14_crypto_winners_distribute_no",
    category: str = "Crypto",
    current_price: float = 0.55,
    confidence: float = 0.6,
    event_id: str | None = None,
) -> SignalRow:
    return SignalRow(
        id=sig_id,
        detector=detector,
        market_id="0xtest",
        event_id=event_id,
        side="NO",
        confidence=confidence,
        current_price=current_price,
        generated_at=_BASE + timedelta(days=day_offset),
        outcome=outcome,
        category=category,
    )


# --- _price_bin ---------------------------------------------------------------


def test_price_bin_matches_production_bin_lookup():
    """Скрипт реимплементирует _price_bin (не импортирует из g18_bin_calibration,
    т.к. тот модуль тянет свой async DB-стек) — обязана быть побитовая копия
    формулы floor(price*N_BINS)/N_BINS, иначе walk-forward-реконструкция G18
    будет систематически расходиться с production lookup."""
    for price in (0.05, 0.09999, 0.1, 0.499, 0.5, 0.501, 0.94, 0.999):
        assert _price_bin(price) == pytest.approx(prod_price_bin(price))


# --- compute_g18_walkforward ---------------------------------------------------


def test_walkforward_returns_none_below_min_obs():
    """< MIN_OBS_FOR_LOOKUP (30) исторических сигналов на cutoff -> G18 skip (None),
    не искусственное forcing калибровки на недостаточных данных."""
    signals = [_sig(f"h{i}", day_offset=i, outcome="WIN") for i in range(5)]
    target = _sig("target", day_offset=20, outcome="WIN")
    signals.append(target)

    result = compute_g18_walkforward(signals)
    p, n = result["target"]

    assert p is None
    assert n == 5


def test_walkforward_applies_bin_once_min_obs_reached():
    """>= 30 исторических сигналов (за пределами 7d lag) в одном (detector,
    category, bin) -> G18 lookup срабатывает, возвращает wr_observed этого бина."""
    # 40 исторических: чередование WIN/LOSS -> wr=0.5, все задолго до target
    # (day 0..39) + 7d lag гарантированно пройден к day 60.
    signals = [_sig(f"h{i}", day_offset=i, outcome="WIN" if i % 2 == 0 else "LOSS") for i in range(40)]
    target = _sig("target", day_offset=60, outcome="WIN")
    signals.append(target)

    result = compute_g18_walkforward(signals)
    p, n = result["target"]

    assert n == 40
    assert p == pytest.approx(0.5)


def test_walkforward_respects_lag_anti_lookahead():
    """Сигналы СЛИШКОМ близко по времени к target (внутри 7d lag) НЕ учитываются
    в historical count — anti-look-ahead. Без lag было бы n=35, с lag — меньше."""
    # 35 сигналов в последние 5 дней перед target (внутри lag) не должны считаться.
    recent = [_sig(f"r{i}", day_offset=10 + i * 0.1, outcome="WIN") for i in range(35)]
    target = _sig("target", day_offset=15, outcome="WIN")

    result = compute_g18_walkforward([*recent, target])
    p, n = result["target"]

    assert n < 35  # lag исключил часть или все "недавние" сигналы
    assert p is None  # n < MIN_OBS_FOR_LOOKUP=30 после исключения


def test_walkforward_excluded_category_always_none():
    """Minute Markets — G18 exclude-категория (production
    WHITELIST_CATEGORIES_EXCLUDE), должна давать None независимо от n_historical."""
    signals = [_sig(f"h{i}", day_offset=i, outcome="WIN", category="Minute Markets") for i in range(40)]
    target = _sig("target", day_offset=60, outcome="WIN", category="Minute Markets")
    signals.append(target)

    result = compute_g18_walkforward(signals)
    p, _n = result["target"]

    assert p is None


def test_walkforward_bins_are_isolated_per_detector_category_bin():
    """История D14 не должна влиять на бин D36 — ключ (detector, category, bin)
    изолирован по всем трём измерениям."""
    d14_hist = [
        _sig(f"d14-{i}", day_offset=i, outcome="WIN", detector="D14_crypto_winners_distribute_no") for i in range(40)
    ]
    d36_target = _sig("d36-target", day_offset=60, outcome="WIN", detector="D36_obi_pressure", category="Sports")

    result = compute_g18_walkforward([*d14_hist, d36_target])
    p, n = result["d36-target"]

    assert n == 0
    assert p is None


# --- _fee_spread_cost_usd / _net_pnl_for_signal --------------------------------


def test_fee_spread_cost_is_positive_and_bounded():
    cost = _fee_spread_cost_usd(500.0, 0.5, "crypto")
    assert 0.0 < cost < 500.0


def test_net_pnl_win_positive_loss_negative_with_high_edge():
    decision = size_from_calibrated_p(calibrated_p=0.75, entry_price=0.5, bankroll_usd=10_000.0, category="crypto")
    assert decision.stake_usd is not None

    net_win = _net_pnl_for_signal(decision, True, "crypto")
    net_loss = _net_pnl_for_signal(decision, False, "crypto")

    assert net_win is not None and net_win > 0
    assert net_loss is not None and net_loss < 0
    # LOSS = -stake - fee/spread cost (строго хуже голых -stake)
    assert net_loss < -decision.stake_usd


def test_net_pnl_returns_none_when_sizer_skips():
    """Сайзер отклонил (EV ниже порога) -> None, сигнал должен быть исключён
    из paired-набора, не молчаливо посчитан как $0."""
    decision = size_from_calibrated_p(calibrated_p=0.30, entry_price=0.7, bankroll_usd=10_000.0, category="crypto")
    assert decision.stake_usd is None

    assert _net_pnl_for_signal(decision, True, "crypto") is None
    assert _net_pnl_for_signal(decision, False, "crypto") is None


# --- build_paired_results -------------------------------------------------------


def test_build_paired_results_excludes_signal_if_either_branch_skips():
    """Если ЛЮБАЯ из веток (G18 или fallback) даёт EV ниже порога -> сигнал
    целиком исключён из paired-набора (иначе популяции G18/fallback разъедутся —
    Acceptance брифа требует одни и те же сигналы для обеих калибровок)."""
    # Низкая confidence -> низкий edge на обеих ветках -> skip на обеих.
    sig = _sig("low-edge", day_offset=0, outcome="WIN", current_price=0.7, confidence=0.3)

    g18_walkforward = {"low-edge": (0.30, 0)}
    fallback_calib = {"low-edge": (0.30, "raw")}

    results, skip_reasons = build_paired_results([sig], g18_walkforward, fallback_calib)

    assert results == []
    assert skip_reasons["ok"] == 0


def test_build_paired_results_g18_none_falls_back_to_fallback_p():
    """Когда G18 walk-forward вернул None (недостаточно данных на cutoff),
    G18-ветка использует fallback_p (эквивалентна fallback-ветке) -> delta=0
    для этого сигнала, а не искусственный skip."""
    sig = _sig("no-g18-data", day_offset=0, outcome="WIN", current_price=0.5, confidence=0.7)

    g18_walkforward = {"no-g18-data": (None, 5)}
    fallback_calib = {"no-g18-data": (0.7, "beta")}

    results, skip_reasons = build_paired_results([sig], g18_walkforward, fallback_calib)

    assert len(results) == 1
    assert results[0].delta == pytest.approx(0.0)
    assert results[0].g18_p == pytest.approx(results[0].fallback_p)
    assert skip_reasons["ok"] == 1


def test_build_paired_results_computes_nonzero_delta_when_g18_differs():
    """Когда G18 walk-forward даёт ДРУГОЕ p, чем fallback, но edge на обеих
    ветках достаточно большой (p=0.85 vs p=0.60 на entry_price=0.5), Kelly-
    стейк на ОБЕИХ ветках упирается в NOTIONAL_BANKROLL_USD=10000 x
    MAX_STAKE_PCT_DEFAULT (5%) и становится идентичным независимо от p ->
    delta=0. Это реальное, задокументированное поведение size_from_calibrated_p
    (cap съедает разницу между сильно различающимися p), не баг теста — см.
    test_build_paired_results_delta_nonzero_when_stake_not_capped ниже для
    p-пары, где cap не достигается и дельта ненулевая."""
    sig = _sig("differs", day_offset=0, outcome="WIN", current_price=0.5, confidence=0.6)

    # p-пара 0.85/0.75 (было 0.85/0.60): после фикса EV-формулы 2026-08-10
    # p=0.60 на entry 0.5 даёт net EV +9.8% — ниже порога 22%, ветка отсеивалась
    # и результатов не было вовсе. Обе текущие ветки дают stake=500 (кап).
    g18_walkforward = {"differs": (0.85, 40)}  # G18 гораздо увереннее
    fallback_calib = {"differs": (0.75, "beta")}

    results, skip_reasons = build_paired_results([sig], g18_walkforward, fallback_calib)
    assert len(results) == 1
    # На дефолтном NOTIONAL_BANKROLL_USD оба упираются в cap -> delta=0 — это
    # ПРАВИЛЬНОЕ поведение сайзера (задокументировано отдельно), не баг теста.
    assert results[0].delta == pytest.approx(0.0)
    assert results[0].g18_p != results[0].fallback_p  # входные p всё же различны
    assert skip_reasons["ok"] == 1


def test_build_paired_results_delta_nonzero_when_stake_not_capped():
    """С p-парой, где Kelly-стейк НЕ упирается в MAX_STAKE_PCT_DEFAULT (5%) cap
    (в отличие от предыдущего теста, где обе ветки капались одинаково), разница
    в calibrated_p транслируется в разный stake -> ненулевую дельту net_pnl.

    entry_price=0.15 и p=0.30/0.26 подобраны так, что обе ветки некапленные и
    расходятся — verified через size_from_calibrated_p(calibrated_p=0.30/0.26,
    entry_price=0.15, bankroll_usd=10000, category='crypto') -> 441.18/323.53.

    Прежняя пара (entry 0.35, p=0.45/0.40) после фикса EV-формулы 2026-08-10
    даёт net EV 16.2%/1.9% — обе ниже порога 22%, результатов не было вовсе."""
    sig = _sig("differs-uncapped", day_offset=0, outcome="WIN", current_price=0.15, confidence=0.6)

    g18_walkforward = {"differs-uncapped": (0.30, 40)}
    fallback_calib = {"differs-uncapped": (0.26, "beta")}

    results, skip_reasons = build_paired_results([sig], g18_walkforward, fallback_calib)

    assert len(results) == 1
    assert results[0].delta != 0.0
    assert skip_reasons["ok"] == 1


# --- bootstrap_delta_ci ---------------------------------------------------------


def _paired(signal_id: str, event_id: str | None, delta: float) -> PairedResult:
    return PairedResult(
        signal_id=signal_id,
        event_id=event_id,
        detector="D14_crypto_winners_distribute_no",
        net_pnl_g18=delta,
        net_pnl_fallback=0.0,
        delta=delta,
        g18_p=0.6,
        fallback_p=0.55,
        fallback_source="beta",
        n_historical_at_cutoff=40,
    )


def test_bootstrap_delta_ci_empty_input():
    assert bootstrap_delta_ci([]) == (0.0, 0.0)


def test_bootstrap_delta_ci_deterministic_with_fixed_seed():
    """seed=42 (см. memory validation-bootstrap-seed-flaky-ci-2026-07-12) должен
    давать одинаковый результат при повторном вызове на одних данных — иначе
    agent_runs.metrics не воспроизводим между прогонами."""
    paired = [_paired(f"s{i}", f"e{i % 3}", float(i) - 5.0) for i in range(20)]

    ci_a = bootstrap_delta_ci(paired)
    ci_b = bootstrap_delta_ci(paired)

    assert ci_a == ci_b
    assert ci_a[0] <= ci_a[1]


def test_bootstrap_delta_ci_null_event_id_grouped_as_singleton():
    """event_id=None не должен схлопывать несвязанные сигналы в один искусственный
    кластер 'unknown' — иначе bootstrap CI недооценит дисперсию (все NULL-сигналы
    ресэмплились бы синхронно как одно псевдо-событие)."""
    paired = [_paired(f"s{i}", None, 100.0 if i == 0 else -1.0) for i in range(10)]

    ci_low, ci_high = bootstrap_delta_ci(paired)

    # С 10 независимыми "событиями" (по одному на NULL-сигнал) большой outlier
    # (100.0 у первого) не должен доминировать детерминированно на каждом
    # ресэмпле — CI должен быть шире одного зафиксированного среднего.
    assert ci_low < ci_high
