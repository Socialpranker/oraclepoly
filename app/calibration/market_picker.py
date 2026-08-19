"""Layer B — Market+Side Picker модель.

Stage 3 / Two-Stage ML Pipeline.

Per-row model (1 row = 1 detector signal). Conditional model: предполагает
что Layer A уже отфильтровал event'ы (p_event > threshold), здесь — какой
market+side выбрать ВНУТРИ event'а.

Признаки (per-row):
- detector_id (one-hot): какой детектор сработал
- calibrated_p: P из L1 Beta calibrator
- edge_pct: ожидаемый edge детектора
- entry_price, horizon_sec, volume_24h: market features
- is_v2_post: pre/post V2 cutover

Event context (carryover из Layer A):
- p_event: вероятность от Layer A (не sample, а conditional context)
- archetype (one-hot)
- n_markets_in_event

Target (mode-зависимо):
- mode='binary': outcome ∈ {0,1}, predict P(WIN). Legacy путь.
- mode='ev_regression': outcome = realized_roi_net / 100 (после Polymarket fee+slippage,
  по сути net PnL на $1 stake). LightGBM regression, predict ожидаемый net ROI.
  Это — то что мы реально хотим: NO @ 0.99 даёт expected_roi ≈ -0.5,
  D5 NO @ 0.05 даёт expected_roi ≈ +5.0. Direct optimization цели.

Critical training properties:
- GroupKFold по event_id — markets одного event'а в одной части (no leakage)
- sample_weight = 1/event_size — каждый event голосует одним голосом
- Per-event Brier (не per-row) для binary; per-event MAE для ev_regression
- Top-1 per-event accuracy: модель выбрала winner внутри event'а?
"""

from __future__ import annotations

import json
from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Any

import lightgbm as lgb
import numpy as np

from app.calibration.event_aware_split import event_aware_holdout_split
from app.calibration.per_event_metrics import per_event_brier

KNOWN_DETECTORS_B = (
    # Mirror MetaLabeler KNOWN_DETECTORS — единый источник правды.
    # Killed-детекторы (D10b/D41/D_esports) оставлены намеренно — см. meta_labeler.py.
    "D3_cross_mkt_arb",
    "D10b_election_ladder",
    "D13_retail_confidence_fade",
    "D14_crypto_winners_distribute_no",
    "D26_fade_whale_extreme",
    "D29_negrisk_basket_arb",
    "D36_obi_pressure",
    "D41_momentum",
    "D_esports_favourite_fade",
    "STANDALONE_V2_LGB",
)
KNOWN_CATEGORIES_B = (
    "Politics",
    "Sports",
    "Crypto",
    "Macroeconomics",
    "Tech & AI",
    "World & Economy",
    "Weather",
    "Other",
)
KNOWN_ARCHETYPES_B = (
    "mutually_exclusive_ladder",
    "one_of_n_choice",
    "independent_multi",
    "hierarchical_time",
    "single_market",
    "unclassified",
)

DEFAULT_HYPERPARAMS_B: dict[str, Any] = {
    "objective": "binary",
    "metric": "binary_logloss",
    "learning_rate": 0.05,
    "num_leaves": 31,
    "max_depth": 6,
    "min_data_in_leaf": 20,
    "feature_fraction": 0.9,
    "bagging_fraction": 0.8,
    "bagging_freq": 5,
    "verbose": -1,
}

# Hyperparams для regression mode — predicts net_roi (на $1 stake, dimensionless float).
# huber loss робастна к outliers (longshot wins дают ROI=+10, но n мало).
DEFAULT_HYPERPARAMS_B_EV: dict[str, Any] = {
    "objective": "huber",
    "alpha": 0.9,  # huber quantile — робастно к heavy-tail
    "metric": "huber",
    "learning_rate": 0.05,
    "num_leaves": 31,
    "max_depth": 6,
    "min_data_in_leaf": 20,
    "feature_fraction": 0.9,
    "bagging_fraction": 0.8,
    "bagging_freq": 5,
    "verbose": -1,
}

# Cap на target значение чтобы один longshot WIN не доминировал loss.
# Обоснование: при entry=0.05 payout = 19, max realized_roi = +1900%. Делим на 100,
# clip(-1.0, 5.0) — отрезаем верхушку (>+500% ROI = 1 на $0.20 token: редко и шумно).
EV_TARGET_CLIP_LO = -1.0
EV_TARGET_CLIP_HI = 5.0


@dataclass(frozen=True)
class MarketPickerExample:
    """Per-row для Layer B (1 row = 1 detector signal).

    Двойной target:
    - `outcome` — binary 0/1 для legacy mode='binary'.
    - `net_roi_target` — float (net_roi после fee+slippage / 100) для mode='ev_regression'.
      Если None в ev_regression → train_market_picker raises ValueError.
    """

    detector_id: str
    calibrated_p: float
    edge_pct: float
    entry_price: float
    horizon_sec: int
    category: str | None
    volume_24h: float
    is_v2_post: bool
    p_event: float  # carryover from Layer A
    archetype: str | None
    n_markets_in_event: int
    outcome: int
    net_roi_target: float | None = None  # для mode='ev_regression'; None в legacy


@dataclass
class MarketPickerConfig:
    hyperparams: dict[str, Any] = field(default_factory=lambda: dict(DEFAULT_HYPERPARAMS_B))
    num_boost_rounds: int = 200
    early_stopping_rounds: int | None = 25
    min_train_rows: int = 100
    mode: str = "binary"  # "binary" | "ev_regression"

    @classmethod
    def for_ev_regression(cls) -> MarketPickerConfig:
        return cls(
            hyperparams=dict(DEFAULT_HYPERPARAMS_B_EV),
            mode="ev_regression",
        )


_BASE_FEATURES_B = [
    "calibrated_p",
    "edge_pct",
    "entry_price",
    "horizon_sec",
    "volume_24h",
    "is_v2_post",
    "p_event",
    "n_markets_in_event",
]


def _vectorize_b(
    examples: list[MarketPickerExample],
    mode: str = "binary",
) -> tuple[np.ndarray, np.ndarray, list[str]]:
    feature_names: list[str] = (
        list(_BASE_FEATURES_B)
        + [f"detector_{d}" for d in KNOWN_DETECTORS_B]
        + [f"category_{c}" for c in KNOWN_CATEGORIES_B]
        + [f"archetype_{a}" for a in KNOWN_ARCHETYPES_B]
    )
    X = _vectorize_to_schema_b(examples, feature_names)
    if mode == "ev_regression":
        # Clip к [-1, +5] чтобы 1 longshot WIN не доминировал huber loss.
        raw = np.array(
            [float(ex.net_roi_target) if ex.net_roi_target is not None else 0.0 for ex in examples],
            dtype=np.float64,
        )
        y = np.clip(raw, EV_TARGET_CLIP_LO, EV_TARGET_CLIP_HI)
    else:
        y = np.array([int(ex.outcome) for ex in examples], dtype=np.int32)
    return X, y, feature_names


def _vectorize_to_schema_b(examples: list[MarketPickerExample], feature_names: list[str]) -> np.ndarray:
    base_idx = {n: i for i, n in enumerate(feature_names) if n in _BASE_FEATURES_B}
    det_idx = {n: i for i, n in enumerate(feature_names) if n.startswith("detector_")}
    cat_idx = {n: i for i, n in enumerate(feature_names) if n.startswith("category_")}
    arch_idx = {n: i for i, n in enumerate(feature_names) if n.startswith("archetype_")}

    X = np.zeros((len(examples), len(feature_names)), dtype=np.float64)
    for i, ex in enumerate(examples):
        if "calibrated_p" in base_idx:
            X[i, base_idx["calibrated_p"]] = float(ex.calibrated_p)
        if "edge_pct" in base_idx:
            X[i, base_idx["edge_pct"]] = float(ex.edge_pct)
        if "entry_price" in base_idx:
            X[i, base_idx["entry_price"]] = float(ex.entry_price)
        if "horizon_sec" in base_idx:
            X[i, base_idx["horizon_sec"]] = float(ex.horizon_sec)
        if "volume_24h" in base_idx:
            X[i, base_idx["volume_24h"]] = float(ex.volume_24h or 0)
        if "is_v2_post" in base_idx:
            X[i, base_idx["is_v2_post"]] = 1.0 if ex.is_v2_post else 0.0
        if "p_event" in base_idx:
            X[i, base_idx["p_event"]] = float(ex.p_event)
        if "n_markets_in_event" in base_idx:
            X[i, base_idx["n_markets_in_event"]] = float(ex.n_markets_in_event)

        det_key = f"detector_{ex.detector_id}"
        if det_key in det_idx:
            X[i, det_idx[det_key]] = 1.0
        if ex.category:
            ck = f"category_{ex.category}"
            if ck in cat_idx:
                X[i, cat_idx[ck]] = 1.0
        if ex.archetype:
            ak = f"archetype_{ex.archetype}"
            if ak in arch_idx:
                X[i, arch_idx[ak]] = 1.0
    return X


@dataclass
class TrainedMarketPicker:
    booster: lgb.Booster
    feature_names: list[str]
    n_train_rows: int
    n_holdout_rows: int
    n_events_train: int
    n_events_holdout: int
    train_per_event_brier: float  # для binary; для ev_regression — train MAE
    holdout_per_event_brier: float  # для binary; для ev_regression — holdout MAE
    mode: str = "binary"  # "binary" | "ev_regression"
    train_indices: list[int] = field(default_factory=list)
    holdout_indices: list[int] = field(default_factory=list)

    def predict(self, examples: list[MarketPickerExample]) -> np.ndarray:
        """Predict.

        - binary mode: returns P(WIN) in [0, 1].
        - ev_regression mode: returns expected_net_roi (clipped to [-1, +5]).
          E.g. +0.5 = 50% expected net ROI per $1 stake.
        """
        x = _vectorize_to_schema_b(examples, self.feature_names)
        preds = self.booster.predict(x, num_iteration=self.booster.best_iteration)
        return np.asarray(preds, dtype=np.float64)

    def predict_one(self, ex: MarketPickerExample) -> float:
        return float(self.predict([ex])[0])

    def feature_importance(self) -> dict[str, int]:
        importances = self.booster.feature_importance(importance_type="split")
        return dict(zip(self.feature_names, [int(v) for v in importances], strict=True))

    def serialize(self) -> dict[str, Any]:
        return {
            "model_str": self.booster.model_to_string(),
            "feature_names": list(self.feature_names),
            "n_train_rows": int(self.n_train_rows),
            "n_holdout_rows": int(self.n_holdout_rows),
            "n_events_train": int(self.n_events_train),
            "n_events_holdout": int(self.n_events_holdout),
            "train_per_event_brier": float(self.train_per_event_brier),
            "holdout_per_event_brier": float(self.holdout_per_event_brier),
            "mode": self.mode,
        }

    @classmethod
    def deserialize(cls, data: dict[str, Any]) -> TrainedMarketPicker:
        booster = lgb.Booster(model_str=data["model_str"])
        return cls(
            booster=booster,
            feature_names=list(data["feature_names"]),
            n_train_rows=int(data["n_train_rows"]),
            n_holdout_rows=int(data["n_holdout_rows"]),
            n_events_train=int(data["n_events_train"]),
            n_events_holdout=int(data["n_events_holdout"]),
            train_per_event_brier=float(data["train_per_event_brier"]),
            holdout_per_event_brier=float(data["holdout_per_event_brier"]),
            mode=str(data.get("mode", "binary")),  # default binary для старых моделей
        )


def train_market_picker(
    examples: list[MarketPickerExample],
    event_ids: Sequence[str],
    *,
    sample_weight: Sequence[float] | None = None,
    config: MarketPickerConfig | None = None,
    holdout_fraction: float = 0.20,
    seed: int = 42,
) -> TrainedMarketPicker:
    """Train Layer B with event-aware split + sample_weight=1/event_size.

    Args:
        examples: per-row training data.
        event_ids: per-row event_id (length = len(examples)). REQUIRED для GroupKFold.
        sample_weight: optional 1/event_size weighting. Default = uniform 1.0.
        config: MarketPickerConfig. mode='binary' (default) или 'ev_regression'.
    """
    cfg = config or MarketPickerConfig()
    if len(examples) < cfg.min_train_rows:
        raise ValueError(f"N={len(examples)} < min_train_rows={cfg.min_train_rows}")
    if len(examples) != len(event_ids):
        raise ValueError(f"len mismatch: examples={len(examples)} event_ids={len(event_ids)}")
    if cfg.mode not in ("binary", "ev_regression"):
        raise ValueError(f"unknown mode: {cfg.mode}")
    if cfg.mode == "ev_regression":
        missing = sum(1 for ex in examples if ex.net_roi_target is None)
        if missing:
            raise ValueError(
                f"ev_regression requires net_roi_target for all rows; missing in {missing}/{len(examples)}"
            )

    train_idx, holdout_idx = event_aware_holdout_split(event_ids, holdout_fraction=holdout_fraction, seed=seed)
    if not train_idx or not holdout_idx:
        raise ValueError("event-aware split produced empty train or holdout")

    train_examples = [examples[i] for i in train_idx]
    holdout_examples = [examples[i] for i in holdout_idx]

    if sample_weight is None:
        train_w = np.ones(len(train_idx), dtype=np.float64)
        holdout_w = np.ones(len(holdout_idx), dtype=np.float64)
    else:
        train_w = np.array([sample_weight[i] for i in train_idx], dtype=np.float64)
        holdout_w = np.array([sample_weight[i] for i in holdout_idx], dtype=np.float64)

    x_train, y_train, feature_names = _vectorize_b(train_examples, mode=cfg.mode)
    x_holdout, y_holdout, _ = _vectorize_b(holdout_examples, mode=cfg.mode)

    train_set = lgb.Dataset(x_train, label=y_train, weight=train_w, feature_name=feature_names)
    valid_set = lgb.Dataset(x_holdout, label=y_holdout, weight=holdout_w, reference=train_set)

    callbacks: list = []
    if cfg.early_stopping_rounds:
        callbacks.append(lgb.early_stopping(cfg.early_stopping_rounds, verbose=False))
    callbacks.append(lgb.log_evaluation(period=0))

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

    train_event_ids = [event_ids[i] for i in train_idx]
    holdout_event_ids = [event_ids[i] for i in holdout_idx]
    if cfg.mode == "binary":
        train_metric = per_event_brier(train_event_ids, train_pred.tolist(), y_train.tolist())
        holdout_metric = per_event_brier(holdout_event_ids, holdout_pred.tolist(), y_holdout.tolist())
    else:
        # ev_regression: per-row MAE (per-event aggregation бессмысленен для continuous target).
        train_metric = float(np.mean(np.abs(train_pred - y_train)))
        holdout_metric = float(np.mean(np.abs(holdout_pred - y_holdout)))

    return TrainedMarketPicker(
        booster=booster,
        feature_names=feature_names,
        n_train_rows=len(train_examples),
        n_holdout_rows=len(holdout_examples),
        n_events_train=len(set(train_event_ids)),
        n_events_holdout=len(set(holdout_event_ids)),
        train_per_event_brier=train_metric,
        holdout_per_event_brier=holdout_metric,
        mode=cfg.mode,
        train_indices=list(train_idx),
        holdout_indices=list(holdout_idx),
    )


def top1_per_event_accuracy(
    event_ids: Sequence[str],
    pred_prob: Sequence[float],
    outcome: Sequence[int],
) -> float:
    """Для каждого event'а: модель выбирает row с max(pred_prob).

    Hit = тот row имеет outcome=1.
    accuracy = mean(hit) по events с хотя бы одним WIN (иначе пропускается).

    Метрика отвечает на: "выбирает ли модель winner внутри event'а".
    """
    rows_by_event: dict[str, list[tuple[float, int]]] = {}
    for ev, p, y in zip(event_ids, pred_prob, outcome, strict=True):
        rows_by_event.setdefault(ev, []).append((float(p), int(y)))

    hits = 0
    n_eligible = 0
    for _, rows in rows_by_event.items():
        if not any(y == 1 for _, y in rows):
            continue  # event без winners — нечего угадывать
        n_eligible += 1
        max_row = max(rows, key=lambda r: r[0])
        if max_row[1] == 1:
            hits += 1
    return hits / n_eligible if n_eligible > 0 else 0.0


def serialize_to_json(model: TrainedMarketPicker) -> str:
    return json.dumps(model.serialize())


def deserialize_from_json(payload: str) -> TrainedMarketPicker:
    return TrainedMarketPicker.deserialize(json.loads(payload))
