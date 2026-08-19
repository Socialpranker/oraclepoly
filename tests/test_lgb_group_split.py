"""Тесты фикса group-leakage в LGB-калибраторе (memorization рынка).

Покрывает:
- _group_split_indices: рынок НИКОГДА не разрезается между train/holdout
  (все строки одного market_id целиком в одну сторону).
- _should_promote ceiling: too-perfect holdout (AUC=1.0 / brier≈0 / мало рынков)
  НЕ промоутится (это leak-signature, инцидент @72h 2026-06-02).
"""

from __future__ import annotations

from app.jobs.ml_calibrator_lgb_fit import (
    MAX_HOLDOUT_AUC,
    _group_split_indices,
    _should_promote,
)

# ── _group_split_indices: рынок не разрезается ───────────────────────────────


def test_market_never_split_across_train_holdout():
    # 5 рынков по 16 строк каждый (имитация ~16 переоценок 72h-рынка).
    market_ids = []
    for m in range(5):
        market_ids += [f"market_{m}"] * 16
    train_idx, holdout_idx = _group_split_indices(market_ids)

    train_markets = {market_ids[i] for i in train_idx}
    holdout_markets = {market_ids[i] for i in holdout_idx}
    # КЛЮЧЕВОЕ: пересечения нет — рынок целиком в одной стороне.
    assert train_markets.isdisjoint(holdout_markets)
    # Все строки покрыты.
    assert len(train_idx) + len(holdout_idx) == len(market_ids)
    # Каждый рынок целиком: все 16 строк рынка в одной группе.
    for m in range(5):
        idxs = {i for i, mid in enumerate(market_ids) if mid == f"market_{m}"}
        assert idxs.issubset(set(train_idx)) or idxs.issubset(set(holdout_idx))


def test_holdout_gets_last_markets_chronologically():
    # Рынки в порядке появления: holdout = последние ~20% уникальных.
    market_ids = []
    for m in range(10):
        market_ids += [f"m{m}"] * 5  # 10 рынков
    _train_idx, holdout_idx = _group_split_indices(market_ids)
    holdout_markets = {market_ids[i] for i in holdout_idx}
    # 20% от 10 = 2 рынка, последние по появлению → m8, m9.
    assert holdout_markets == {"m8", "m9"}


def test_single_market_all_train():
    # Один рынок → не из чего делать holdout → всё в train.
    market_ids = ["only"] * 30
    train_idx, holdout_idx = _group_split_indices(market_ids)
    assert len(train_idx) == 30
    assert holdout_idx == []


def test_two_markets_one_to_holdout():
    market_ids = ["a"] * 10 + ["b"] * 10
    train_idx, holdout_idx = _group_split_indices(market_ids)
    # max(1, int(2*0.2))=1 рынок в holdout → последний (b).
    assert {market_ids[i] for i in holdout_idx} == {"b"}
    assert {market_ids[i] for i in train_idx} == {"a"}


# ── _should_promote ceiling: too-perfect не промоутится ──────────────────────


def _base_kwargs(**over):
    kw = dict(
        detector="STANDALONE_V2_LGB@72h",
        holdout_brier=0.10,
        brier_raw_p=0.13,
        brier_beta=0.12,
        n_holdout=400,
        holdout_auc=0.85,
        n_holdout_markets=50,
    )
    kw.update(over)
    return kw


def test_ceiling_rejects_perfect_auc():
    # AUC=1.0 → memorization signature → no promote (даже если brier "отличный").
    assert _should_promote(**_base_kwargs(holdout_auc=1.0, holdout_brier=0.0001)) is False


def test_ceiling_rejects_auc_above_max():
    assert _should_promote(**_base_kwargs(holdout_auc=MAX_HOLDOUT_AUC + 0.001)) is False


def test_ceiling_rejects_too_low_brier():
    # brier ниже пола → подозрительно идеально.
    assert _should_promote(**_base_kwargs(holdout_brier=0.01)) is False


def test_ceiling_rejects_few_holdout_markets():
    # holdout из горстки рынков — нельзя верить.
    assert _should_promote(**_base_kwargs(n_holdout_markets=5)) is False


def test_healthy_model_still_promotes():
    # Здоровая модель (AUC 0.85, brier 0.10 < beta 0.12, много рынков) → promote.
    assert _should_promote(**_base_kwargs()) is True


def test_ceiling_does_not_block_when_markets_zero():
    # n_holdout_markets=0 (не передан/не считается) — guard `0 < markets < MIN` не срабатывает.
    # Backward-compat: старые вызовы без n_holdout_markets не ломаются.
    assert _should_promote(**_base_kwargs(n_holdout_markets=0)) is True
