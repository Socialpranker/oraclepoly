"""L2 LightGBM stacking meta-labeler.

Поверх L1 Beta calibrators per-detector — глобальная модель, которая:
1. Принимает фичи `{detector_id, calibrated_p, edge_pct, market features}`
2. Учит non-linear interactions (overlap D1+D3 на одном market, regime shifts)
3. Выдаёт final P[trade ROI > 0]

Архитектура (Lopez de Prado meta-labeling):
- Primary signal = (detector, side) — направление
- Meta = P[trade succeeds] — confidence/size

Walk-forward purged + embargoed (избегаем lookhead через resolution time overlap).

Используется в Phase E.4 EV-aware execution:
    ev = p * (1 - entry_price) - (1 - p) * entry_price
    if ev / entry_price < min_edge_pct: skip
    else: stake = bankroll * fractional_kelly_with(p, edge)
"""

from __future__ import annotations

import json
from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Any

import lightgbm as lgb
import numpy as np

DEFAULT_HYPERPARAMS: dict[str, Any] = {
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


@dataclass(frozen=True)
class MetaLabelerExample:
    """Одна обучающая строка для L2 meta-labeler.

    Признаки:
    - detector_id: 'D1_fade_whale' | 'D3_cross_mkt_arb' | ...
    - calibrated_p: P из L1 Beta (либо raw confidence если L1 не fit'ed)
    - edge_pct: ожидаемый edge из бэктеста (захардкожен в детекторе)
    - entry_price: current_price на момент сигнала
    - horizon_sec: TTL сигнала
    - category: market category ("Politics", "Sports", etc) — может быть None
    - volume_24h: объём маркета на момент сигнала
    - is_v2_post: True если signal generated_at > 2026-04-28 (V2 cutover)

    Target:
    - outcome: 1 = WIN, 0 = LOSS
    """

    detector_id: str
    calibrated_p: float
    edge_pct: float
    entry_price: float
    horizon_sec: int
    category: str | None
    volume_24h: float
    is_v2_post: bool
    outcome: int
    event_id: str | None = None  # для GroupKFold; не входит в фичи модели
    # Tier 3.1 фичи (дефолт None → NaN в X; LGB обрабатывает NaN нативно).
    # None, а не 0.0: 0 это валидное значение (recent_wr_30d=0 → детектор с 0% WR).
    hours_to_resolution: float | None = None  # часы до end_date на момент сигнала
    recent_wr_30d: float | None = None  # rolling WR детектора <30d strictly до generated_at
    settle_velocity_hours: float | None = None  # медиана settle-времени (detector,category)


# Whitelist допустимых значений для детерминированного one-hot.
#
# 2026-05-19: расширено с 4 до 16 active детекторов после аудита ML стека.
# Раньше большинство детекторов (D31, D14, D36, D42, STANDALONE, ...) слипались
# в "other detector" bucket → L2 не мог per-detector штрафовать. Теперь каждый
# active детектор имеет own one-hot feature.
#
# Inference path использует _vectorize_to_schema(feature_names самой модели) для
# backward compat — старые модели с малым KNOWN продолжат работать без
# регрессии. Новые сигналы с unknown detector_id → all-zero one-hot.
# NB: killed-детекторы (D10b/D41/D_esports, снесены 2026-06-20) НАМЕРЕННО остаются
# в списке — это one-hot feature-схема, удаление сдвинуло бы её и рассинхронило с
# уже сериализованными моделями. Killed дают all-zero колонку (нет сигналов), безвредны.
KNOWN_DETECTORS = (
    # Rules-based detectors
    "D3_cross_mkt_arb",
    "D10b_election_ladder",
    "D13_retail_confidence_fade",
    "D14_crypto_winners_distribute_no",
    "D26_fade_whale_extreme",
    "D29_negrisk_basket_arb",
    "D36_obi_pressure",
    "D41_momentum",
    "D_esports_favourite_fade",
    # ML-based detectors
    "STANDALONE_V2_LGB",
)
KNOWN_CATEGORIES = (
    "Politics",
    "Sports",
    "Crypto",
    "Macroeconomics",
    "Tech & AI",
    "World & Economy",
    "Weather",
    "Other",
)


@dataclass
class MetaLabelerConfig:
    """Конфигурация обучения L2 meta-labeler."""

    hyperparams: dict[str, Any] = field(default_factory=lambda: dict(DEFAULT_HYPERPARAMS))
    num_boost_rounds: int = 200
    early_stopping_rounds: int | None = 25
    min_train_samples: int = 100


# Базовые скалярные фичи модели. Расширяются + KNOWN_DETECTORS one-hot + KNOWN_CATEGORIES one-hot.
#
# Tier 3.1 (2026-06-14): добавлены hours_to_resolution / recent_wr_30d /
# settle_velocity_hours. SQL для них — в fit-джобах (ml_meta_labeler_fit,
# ml_honest_baseline_fit) и inference (detector_signal_service). None → NaN
# (LGB обрабатывает нативно). depth_l5_usd НЕ добавлен — нет данных книги в БД,
# proxy volume_24h×0.05 дублирует существующую volume_24h. backward compat:
# _vectorize_to_schema(self.feature_names) — старые модели без новых фич работают.
_BASE_FEATURES = [
    "calibrated_p",
    "edge_pct",
    "entry_price",
    "horizon_sec",
    "volume_24h",
    "is_v2_post",
    "hours_to_resolution",
    "recent_wr_30d",
    "settle_velocity_hours",
]


def _vectorize(examples: list[MetaLabelerExample]) -> tuple[np.ndarray, np.ndarray, list[str]]:
    """Превращает примеры в (X, y, feature_names) по ТЕКУЩИМ KNOWN_*.

    Используется при train. При predict — _vectorize_to_schema (backward compat).
    """
    feature_names: list[str] = (
        _BASE_FEATURES + [f"detector_{d}" for d in KNOWN_DETECTORS] + [f"category_{c}" for c in KNOWN_CATEGORIES]
    )
    X = _vectorize_to_schema(examples, feature_names)
    y = np.array([int(ex.outcome) for ex in examples], dtype=np.int32)
    return X, y, feature_names


def _vectorize_to_schema(examples: list[MetaLabelerExample], feature_names: list[str]) -> np.ndarray:
    """Vectorize по конкретному feature_names ordering.

    Позволяет inference на старых моделях даже после изменения KNOWN_DETECTORS:
    модель сама хранит свой feature_names, мы строим X в её схеме.
    Незнакомые detector_id или category → all-zero one-hot (treated as "other").
    """
    detector_features: dict[str, int] = {
        name: i for i, name in enumerate(feature_names) if name.startswith("detector_")
    }
    category_features: dict[str, int] = {
        name: i for i, name in enumerate(feature_names) if name.startswith("category_")
    }
    base_idx: dict[str, int] = {name: i for i, name in enumerate(feature_names) if name in _BASE_FEATURES}

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
            X[i, base_idx["volume_24h"]] = float(ex.volume_24h)
        if "is_v2_post" in base_idx:
            X[i, base_idx["is_v2_post"]] = 1.0 if ex.is_v2_post else 0.0
        # Tier 3.1: None → NaN (НЕ 0.0). LGB трактует NaN как missing и сам
        # выбирает направление сплита; 0.0 был бы валидным значением фичи.
        if "hours_to_resolution" in base_idx:
            X[i, base_idx["hours_to_resolution"]] = (
                float(ex.hours_to_resolution) if ex.hours_to_resolution is not None else np.nan
            )
        if "recent_wr_30d" in base_idx:
            X[i, base_idx["recent_wr_30d"]] = float(ex.recent_wr_30d) if ex.recent_wr_30d is not None else np.nan
        if "settle_velocity_hours" in base_idx:
            X[i, base_idx["settle_velocity_hours"]] = (
                float(ex.settle_velocity_hours) if ex.settle_velocity_hours is not None else np.nan
            )

        det_key = f"detector_{ex.detector_id}"
        if det_key in detector_features:
            X[i, detector_features[det_key]] = 1.0

        if ex.category:
            cat_key = f"category_{ex.category}"
            if cat_key in category_features:
                X[i, category_features[cat_key]] = 1.0

    return X


@dataclass
class TrainedMetaLabeler:
    """Обученная LGB модель + метаданные для inference."""

    booster: lgb.Booster
    feature_names: list[str]
    n_train: int
    n_holdout: int
    train_brier: float
    holdout_brier: float

    def predict(self, examples: list[MetaLabelerExample]) -> np.ndarray:
        """Возвращает P[WIN] на массиве примеров.

        Backward-compatible: vectorize по схеме _этой_ модели (self.feature_names),
        не по текущим KNOWN_DETECTORS. Это позволяет inference на старых моделях
        даже после расширения KNOWN_DETECTORS (D5/D6/...).
        """
        x = _vectorize_to_schema(examples, self.feature_names)
        preds = self.booster.predict(x, num_iteration=self.booster.best_iteration)
        return np.asarray(preds, dtype=np.float64)

    def predict_one(self, example: MetaLabelerExample) -> float:
        return float(self.predict([example])[0])

    def feature_importance(self) -> dict[str, int]:
        importances = self.booster.feature_importance(importance_type="split")
        return dict(zip(self.feature_names, [int(v) for v in importances], strict=True))

    def serialize(self) -> dict[str, Any]:
        return {
            "model_str": self.booster.model_to_string(),
            "feature_names": list(self.feature_names),
            "n_train": int(self.n_train),
            "n_holdout": int(self.n_holdout),
            "train_brier": float(self.train_brier),
            "holdout_brier": float(self.holdout_brier),
        }

    @classmethod
    def deserialize(cls, data: dict[str, Any]) -> TrainedMetaLabeler:
        booster = lgb.Booster(model_str=data["model_str"])
        return cls(
            booster=booster,
            feature_names=list(data["feature_names"]),
            n_train=int(data["n_train"]),
            n_holdout=int(data["n_holdout"]),
            train_brier=float(data["train_brier"]),
            holdout_brier=float(data["holdout_brier"]),
        )


def _brier(y_true: np.ndarray, y_pred: np.ndarray) -> float:
    return float(np.mean((y_pred - y_true) ** 2))


def _event_aware_split(
    examples: list[MetaLabelerExample],
    holdout_fraction: float,
) -> tuple[list[MetaLabelerExample], list[MetaLabelerExample]]:
    """Chronological event-level split.

    Если у ≥50% примеров есть event_id — группируем по event_id, берём последние
    holdout_fraction уникальных событий (по времени первого сигнала). Это предотвращает
    data leakage: маркеты одного события не попадают и в train, и в holdout.

    Иначе — простой хронологический cut по позициям (examples уже отсортированы по времени).
    """
    n_with_event = sum(1 for ex in examples if ex.event_id)
    use_event_split = n_with_event >= len(examples) * 0.5

    if not use_event_split:
        cut = int(len(examples) * (1.0 - holdout_fraction))
        return examples[:cut], examples[cut:]

    # Собираем уникальные события, сортируем по первому появлению (chronological)
    event_first_seen: dict[str, int] = {}
    example_event_ids: list[str] = []
    for idx, ex in enumerate(examples):
        eid = ex.event_id if ex.event_id else f"__no_event__{idx}"
        example_event_ids.append(eid)
        if eid not in event_first_seen:
            event_first_seen[eid] = idx

    sorted_events = sorted(event_first_seen.keys(), key=lambda e: event_first_seen[e])
    cut_event = int(len(sorted_events) * (1.0 - holdout_fraction))
    train_events = set(sorted_events[:cut_event])

    train_examples = [ex for ex, eid in zip(examples, example_event_ids) if eid in train_events]
    holdout_examples = [ex for ex, eid in zip(examples, example_event_ids) if eid not in train_events]
    return train_examples, holdout_examples


def train_meta_labeler(
    examples: list[MetaLabelerExample],
    config: MetaLabelerConfig | None = None,
    holdout_fraction: float = 0.20,
    seed: int = 42,
    *,
    sample_weight: Sequence[float] | None = None,
    groups: Sequence[str] | None = None,
) -> TrainedMetaLabeler:
    """Train + holdout evaluation за один проход.

    Split стратегия (в порядке приоритета):
    1. Если передан `groups=` (per-row event_id) → event_aware_holdout_split с
       детерминированным seed. Поддерживает `sample_weight=` (1/event_size).
       Используется явно из honest_baseline / future ml_meta_labeler_fit.
    2. Иначе если ≥50% examples имеют ex.event_id → inline chronological event-level
       split (`_event_aware_split`). Гарантирует что markets одного event не попадают
       и в train, и в holdout. Этот путь — для legacy ml_meta_labeler_fit cron.
    3. Иначе → position-level chronological cut по последним holdout_fraction.

    examples должны быть отсортированы по generated_at ASC (SQL ORDER BY).

    Args:
        examples: training rows.
        config: MetaLabelerConfig (default = sane defaults).
        holdout_fraction: доля holdout (0..1).
        seed: для детерминизма event_aware_holdout_split (если groups передан).
        sample_weight: опциональный per-row вес. Default = uniform 1.0.
            Используется в Stage 1+ для 1/event_size weighting.
        groups: опциональный per-row event_id. При наличии → используется
            event_aware_holdout_split (markets одного event'а в одной части).
    """
    _ = seed
    cfg = config or MetaLabelerConfig()
    if len(examples) < cfg.min_train_samples:
        raise ValueError(f"N={len(examples)} < min_train_samples={cfg.min_train_samples}")

    n = len(examples)
    if sample_weight is not None and len(sample_weight) != n:
        raise ValueError(f"sample_weight len mismatch: {len(sample_weight)} vs {n}")
    if groups is not None and len(groups) != n:
        raise ValueError(f"groups len mismatch: {len(groups)} vs {n}")

    if groups is not None:
        from app.calibration.event_aware_split import event_aware_holdout_split

        train_idx, holdout_idx = event_aware_holdout_split(groups, holdout_fraction=holdout_fraction, seed=seed)
        train_examples = [examples[i] for i in train_idx]
        holdout_examples = [examples[i] for i in holdout_idx]
        if sample_weight is not None:
            train_w = np.array([sample_weight[i] for i in train_idx], dtype=np.float64)
            holdout_w = np.array([sample_weight[i] for i in holdout_idx], dtype=np.float64)
        else:
            train_w = np.ones(len(train_examples), dtype=np.float64)
            holdout_w = np.ones(len(holdout_examples), dtype=np.float64)
    else:
        # Fallback: derive split из самих examples (event_id field либо chronological).
        # Используется legacy ml_meta_labeler_fit cron, где groups явно не передаётся.
        train_examples, holdout_examples = _event_aware_split(examples, holdout_fraction)
        if sample_weight is not None:
            # sample_weight передан без groups — нужно повторить порядок (same idx mapping)
            train_ids = {id(ex) for ex in train_examples}
            train_w = np.array(
                [sample_weight[i] for i, ex in enumerate(examples) if id(ex) in train_ids],
                dtype=np.float64,
            )
            holdout_w = np.array(
                [sample_weight[i] for i, ex in enumerate(examples) if id(ex) not in train_ids],
                dtype=np.float64,
            )
        else:
            train_w = np.ones(len(train_examples), dtype=np.float64)
            holdout_w = np.ones(len(holdout_examples), dtype=np.float64)

    if not train_examples or not holdout_examples:
        raise ValueError("split produced empty train or holdout")

    x_train, y_train, feature_names = _vectorize(train_examples)
    x_holdout, y_holdout, _ = _vectorize(holdout_examples)

    train_set = lgb.Dataset(x_train, label=y_train, weight=train_w, feature_name=feature_names)
    valid_set = lgb.Dataset(x_holdout, label=y_holdout, weight=holdout_w, reference=train_set)

    callbacks: list = []
    if cfg.early_stopping_rounds:
        callbacks.append(lgb.early_stopping(cfg.early_stopping_rounds, verbose=False))
    callbacks.append(lgb.log_evaluation(period=0))

    booster = lgb.train(
        cfg.hyperparams,
        train_set,
        num_boost_round=cfg.num_boost_rounds,
        valid_sets=[train_set, valid_set],
        callbacks=callbacks,
    )

    train_pred = booster.predict(x_train, num_iteration=booster.best_iteration)
    holdout_pred = booster.predict(x_holdout, num_iteration=booster.best_iteration)
    train_brier = _brier(np.asarray(y_train, dtype=np.float64), np.asarray(train_pred))
    holdout_brier = _brier(np.asarray(y_holdout, dtype=np.float64), np.asarray(holdout_pred))

    return TrainedMetaLabeler(
        booster=booster,
        feature_names=feature_names,
        n_train=len(train_examples),
        n_holdout=len(holdout_examples),
        train_brier=train_brier,
        holdout_brier=holdout_brier,
    )


def serialize_to_json(model: TrainedMetaLabeler) -> str:
    return json.dumps(model.serialize())


def deserialize_from_json(payload: str) -> TrainedMetaLabeler:
    return TrainedMetaLabeler.deserialize(json.loads(payload))
