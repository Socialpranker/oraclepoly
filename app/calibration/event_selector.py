"""Layer A — Event Selector модель.

Stage 2 / Two-Stage ML Pipeline.

Признаки (per-event, 1 row = 1 event):
- category: market category event'а
- archetype: классификация структуры event'а (Stage 0 backfill)
- n_markets: сколько markets в event'е
- volume_24h: объём event'а
- liquidity: ликвидность event'а
- days_to_resolution: дни до резолва (на момент сигнала)
- mean_market_price: средняя цена markets event'а
- market_price_dispersion: stddev цен (signal: насколько markets event'а 'разбросаны')
- neg_risk / neg_risk_other / n_yes_winners: neg-risk metadata
- n_detector_signals: сколько детекторных сигналов сработало (proxy на attention)

Target:
- outcome: 1 если event_roi > 0 (per-event ROI из mv_event_outcome), иначе 0

Time-based split по min(detector_signals.generated_at) per-event.
Walk-forward — старые events в train, новые в holdout.

Layer A != Layer B: здесь честный n=n_events (не inflated). Backward-compat schema
через _vectorize_to_schema_a — модель сама хранит свой feature_names.
"""

from __future__ import annotations

import json
from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Any

import lightgbm as lgb
import numpy as np

KNOWN_ARCHETYPES = (
    "mutually_exclusive_ladder",
    "one_of_n_choice",
    "independent_multi",
    "hierarchical_time",
    "single_market",
    "unclassified",
)
KNOWN_CATEGORIES_A = (
    "Politics",
    "Sports",
    "Crypto",
    "Macroeconomics",
    "Tech & AI",
    "World & Economy",
    "Weather",
    "Other",
)

DEFAULT_HYPERPARAMS_A: dict[str, Any] = {
    "objective": "binary",
    "metric": "binary_logloss",
    "learning_rate": 0.05,
    "num_leaves": 31,
    "max_depth": 6,
    "min_data_in_leaf": 10,
    "feature_fraction": 0.9,
    "bagging_fraction": 0.8,
    "bagging_freq": 5,
    "verbose": -1,
}


@dataclass(frozen=True)
class EventSelectorExample:
    """1 row = 1 event для Layer A."""

    event_id: str
    category: str | None
    archetype: str | None
    n_markets: int
    volume_24h: float
    liquidity: float
    days_to_resolution: float
    mean_market_price: float
    market_price_dispersion: float
    neg_risk: bool
    neg_risk_other: bool
    n_yes_winners: int
    n_detector_signals: int
    outcome: int  # 1 если event_roi > 0


@dataclass
class EventSelectorConfig:
    hyperparams: dict[str, Any] = field(default_factory=lambda: dict(DEFAULT_HYPERPARAMS_A))
    num_boost_rounds: int = 200
    early_stopping_rounds: int | None = 25
    min_train_events: int = 50


_BASE_FEATURES_A = [
    "n_markets",
    "volume_24h",
    "liquidity",
    "days_to_resolution",
    "mean_market_price",
    "market_price_dispersion",
    "neg_risk",
    "neg_risk_other",
    "n_yes_winners",
    "n_detector_signals",
]


def _vectorize_a(examples: list[EventSelectorExample]) -> tuple[np.ndarray, np.ndarray, list[str]]:
    """Vectorize по ТЕКУЩИМ KNOWN_*. Используется при train."""
    feature_names: list[str] = (
        list(_BASE_FEATURES_A)
        + [f"archetype_{a}" for a in KNOWN_ARCHETYPES]
        + [f"category_{c}" for c in KNOWN_CATEGORIES_A]
    )
    X = _vectorize_to_schema_a(examples, feature_names)
    y = np.array([int(ex.outcome) for ex in examples], dtype=np.int32)
    return X, y, feature_names


def _vectorize_to_schema_a(examples: list[EventSelectorExample], feature_names: list[str]) -> np.ndarray:
    """Vectorize по конкретному feature_names ordering (backward-compat для inference).

    Незнакомые archetype/category → all-zero one-hot (treated as 'other').
    """
    base_idx = {n: i for i, n in enumerate(feature_names) if n in _BASE_FEATURES_A}
    arch_idx = {n: i for i, n in enumerate(feature_names) if n.startswith("archetype_")}
    cat_idx = {n: i for i, n in enumerate(feature_names) if n.startswith("category_")}

    X = np.zeros((len(examples), len(feature_names)), dtype=np.float64)
    for i, ex in enumerate(examples):
        if "n_markets" in base_idx:
            X[i, base_idx["n_markets"]] = float(ex.n_markets)
        if "volume_24h" in base_idx:
            X[i, base_idx["volume_24h"]] = float(ex.volume_24h or 0)
        if "liquidity" in base_idx:
            X[i, base_idx["liquidity"]] = float(ex.liquidity or 0)
        if "days_to_resolution" in base_idx:
            X[i, base_idx["days_to_resolution"]] = float(ex.days_to_resolution or 0)
        if "mean_market_price" in base_idx:
            X[i, base_idx["mean_market_price"]] = float(ex.mean_market_price or 0)
        if "market_price_dispersion" in base_idx:
            X[i, base_idx["market_price_dispersion"]] = float(ex.market_price_dispersion or 0)
        if "neg_risk" in base_idx:
            X[i, base_idx["neg_risk"]] = 1.0 if ex.neg_risk else 0.0
        if "neg_risk_other" in base_idx:
            X[i, base_idx["neg_risk_other"]] = 1.0 if ex.neg_risk_other else 0.0
        if "n_yes_winners" in base_idx:
            X[i, base_idx["n_yes_winners"]] = float(ex.n_yes_winners)
        if "n_detector_signals" in base_idx:
            X[i, base_idx["n_detector_signals"]] = float(ex.n_detector_signals)

        if ex.archetype:
            key = f"archetype_{ex.archetype}"
            if key in arch_idx:
                X[i, arch_idx[key]] = 1.0
        if ex.category:
            key = f"category_{ex.category}"
            if key in cat_idx:
                X[i, cat_idx[key]] = 1.0
    return X


@dataclass
class TrainedEventSelector:
    booster: lgb.Booster
    feature_names: list[str]
    n_events_train: int
    n_events_holdout: int
    train_per_event_brier: float
    holdout_per_event_brier: float

    def predict(self, examples: list[EventSelectorExample]) -> np.ndarray:
        x = _vectorize_to_schema_a(examples, self.feature_names)
        preds = self.booster.predict(x, num_iteration=self.booster.best_iteration)
        return np.asarray(preds, dtype=np.float64)

    def predict_one(self, ex: EventSelectorExample) -> float:
        return float(self.predict([ex])[0])

    def feature_importance(self) -> dict[str, int]:
        importances = self.booster.feature_importance(importance_type="split")
        return dict(zip(self.feature_names, [int(v) for v in importances], strict=True))

    def serialize(self) -> dict[str, Any]:
        return {
            "model_str": self.booster.model_to_string(),
            "feature_names": list(self.feature_names),
            "n_events_train": int(self.n_events_train),
            "n_events_holdout": int(self.n_events_holdout),
            "train_per_event_brier": float(self.train_per_event_brier),
            "holdout_per_event_brier": float(self.holdout_per_event_brier),
        }

    @classmethod
    def deserialize(cls, data: dict[str, Any]) -> TrainedEventSelector:
        booster = lgb.Booster(model_str=data["model_str"])
        return cls(
            booster=booster,
            feature_names=list(data["feature_names"]),
            n_events_train=int(data["n_events_train"]),
            n_events_holdout=int(data["n_events_holdout"]),
            train_per_event_brier=float(data["train_per_event_brier"]),
            holdout_per_event_brier=float(data["holdout_per_event_brier"]),
        )


def _per_event_brier_a(y_true: Sequence[int], y_pred: Sequence[float]) -> float:
    """Per-event Brier — 1 row = 1 event, поэтому = mean squared error."""
    yt = np.asarray(y_true, dtype=float)
    yp = np.asarray(y_pred, dtype=float)
    return float(np.mean((yp - yt) ** 2))


def train_event_selector(
    examples: list[EventSelectorExample],
    event_timestamps: Sequence[float],
    config: EventSelectorConfig | None = None,
    holdout_fraction: float = 0.20,
    seed: int = 42,
) -> TrainedEventSelector:
    """Train Layer A on per-event rows.

    Time-based split по event_timestamps (epoch seconds, например min(generated_at)
    для сигналов event'а или event.created_at). Старые events → train, новые → holdout.

    Args:
        examples: per-event rows (1 row = 1 event).
        event_timestamps: per-row timestamp (length = len(examples)).
        config: hyperparams + split config.
        holdout_fraction: доля events в holdout (0..1).
        seed: для определённости (LightGBM stochasticity).
    """
    cfg = config or EventSelectorConfig()
    if len(examples) < cfg.min_train_events:
        raise ValueError(f"N={len(examples)} < min_train_events={cfg.min_train_events}")

    if len(examples) != len(event_timestamps):
        raise ValueError(f"len mismatch: examples={len(examples)} ts={len(event_timestamps)}")

    # Sort by timestamp (ascending), split last `holdout_fraction` as holdout
    order = sorted(range(len(examples)), key=lambda i: event_timestamps[i])
    cut = int(len(order) * (1.0 - holdout_fraction))
    train_idx = order[:cut]
    holdout_idx = order[cut:]
    train_examples = [examples[i] for i in train_idx]
    holdout_examples = [examples[i] for i in holdout_idx]

    if not train_examples or not holdout_examples:
        raise ValueError("split produced empty train or holdout")

    x_train, y_train, feature_names = _vectorize_a(train_examples)
    x_holdout, y_holdout, _ = _vectorize_a(holdout_examples)

    train_set = lgb.Dataset(x_train, label=y_train, feature_name=feature_names)
    valid_set = lgb.Dataset(x_holdout, label=y_holdout, reference=train_set)

    callbacks: list = []
    if cfg.early_stopping_rounds:
        callbacks.append(lgb.early_stopping(cfg.early_stopping_rounds, verbose=False))
    callbacks.append(lgb.log_evaluation(period=0))

    # Set seed for LightGBM determinism
    params = dict(cfg.hyperparams)
    params.setdefault("seed", seed)
    params.setdefault("deterministic", True)

    booster = lgb.train(
        params,
        train_set,
        num_boost_round=cfg.num_boost_rounds,
        valid_sets=[train_set, valid_set],
        callbacks=callbacks,
    )

    train_pred = booster.predict(x_train, num_iteration=booster.best_iteration)
    holdout_pred = booster.predict(x_holdout, num_iteration=booster.best_iteration)
    train_brier = _per_event_brier_a(y_train.tolist(), list(train_pred))
    holdout_brier = _per_event_brier_a(y_holdout.tolist(), list(holdout_pred))

    return TrainedEventSelector(
        booster=booster,
        feature_names=feature_names,
        n_events_train=len(train_examples),
        n_events_holdout=len(holdout_examples),
        train_per_event_brier=train_brier,
        holdout_per_event_brier=holdout_brier,
    )


def serialize_to_json(model: TrainedEventSelector) -> str:
    return json.dumps(model.serialize())


def deserialize_from_json(payload: str) -> TrainedEventSelector:
    return TrainedEventSelector.deserialize(json.loads(payload))
