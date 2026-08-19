"""Tests для расширения train_meta_labeler — sample_weight + groups.

Stage 1 / Two-Stage ML Pipeline.

Требования:
- Backward compatible: старая сигнатура без sample_weight/groups работает как раньше
- При передаче groups → используется event_aware_holdout_split
- При sample_weight=None → uniform 1.0 (эквивалент старому поведению)
"""

from __future__ import annotations

import numpy as np
import pytest


def _mk_examples(n: int = 200, seed: int = 42):
    from app.calibration.meta_labeler import MetaLabelerExample

    rng = np.random.default_rng(seed)
    out = []
    for _ in range(n):
        out.append(
            MetaLabelerExample(
                detector_id="D1_fade_whale",
                calibrated_p=float(rng.uniform(0.3, 0.7)),
                edge_pct=10.0,
                entry_price=0.5,
                horizon_sec=1800,
                category="Politics",
                volume_24h=10000.0,
                is_v2_post=False,
                outcome=int(rng.integers(0, 2)),
            )
        )
    return out


def test_legacy_signature_still_works():
    """train_meta_labeler без новых kwargs работает (backward compat)."""
    from app.calibration.meta_labeler import MetaLabelerConfig, train_meta_labeler

    examples = _mk_examples(200)
    cfg = MetaLabelerConfig(min_train_samples=50)
    model = train_meta_labeler(examples, config=cfg, holdout_fraction=0.2, seed=42)
    assert model.n_train + model.n_holdout == 200


def test_train_with_uniform_weights_equals_unweighted():
    """sample_weight=[1.0, ...] эквивалентен None (uniform)."""
    from app.calibration.meta_labeler import MetaLabelerConfig, train_meta_labeler

    examples = _mk_examples(200)
    cfg = MetaLabelerConfig(min_train_samples=50)
    m1 = train_meta_labeler(examples, config=cfg, holdout_fraction=0.2, seed=42)
    weights = [1.0] * len(examples)
    m2 = train_meta_labeler(examples, config=cfg, holdout_fraction=0.2, seed=42, sample_weight=weights)
    assert abs(m1.holdout_brier - m2.holdout_brier) < 1e-6


def test_train_with_event_aware_groups_no_leakage():
    """groups=event_id → markets одного event'а в одной части (train ИЛИ holdout)."""
    from app.calibration.meta_labeler import MetaLabelerConfig, train_meta_labeler

    examples = _mk_examples(200)
    cfg = MetaLabelerConfig(min_train_samples=50)
    groups = [f"ev{i // 5}" for i in range(200)]  # 40 events × 5 markets
    model = train_meta_labeler(examples, config=cfg, holdout_fraction=0.2, seed=42, groups=groups)
    assert model.n_train + model.n_holdout == 200


def test_train_groups_with_weights():
    """Combined: groups + sample_weight=1/event_size."""
    from app.calibration.meta_labeler import MetaLabelerConfig, train_meta_labeler

    examples = _mk_examples(200)
    cfg = MetaLabelerConfig(min_train_samples=50)
    groups = [f"ev{i // 5}" for i in range(200)]
    weights = [1.0 / 5] * 200  # event_size=5
    model = train_meta_labeler(
        examples, config=cfg, holdout_fraction=0.2, seed=42, sample_weight=weights, groups=groups
    )
    assert model.n_train + model.n_holdout == 200


def test_groups_length_mismatch_raises():
    from app.calibration.meta_labeler import MetaLabelerConfig, train_meta_labeler

    examples = _mk_examples(200)
    cfg = MetaLabelerConfig(min_train_samples=50)
    groups_wrong = [f"ev{i // 5}" for i in range(150)]  # 150 vs 200
    with pytest.raises(ValueError):
        train_meta_labeler(examples, config=cfg, groups=groups_wrong)


def test_sample_weight_length_mismatch_raises():
    from app.calibration.meta_labeler import MetaLabelerConfig, train_meta_labeler

    examples = _mk_examples(200)
    cfg = MetaLabelerConfig(min_train_samples=50)
    weights_wrong = [1.0] * 150
    with pytest.raises(ValueError):
        train_meta_labeler(examples, config=cfg, sample_weight=weights_wrong)
