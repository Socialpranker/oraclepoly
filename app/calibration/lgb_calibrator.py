"""Per-detector LightGBM calibrator — параллельная реализация к BetaCalibrator.

Контекст 2026-05-19 (D7b/D41 pilot):
    Некоторые детекторы (D31, D41, D33) выдают **constant** raw confidence
    (stddev=0). Beta calibrator (2 params, 1 input feature) не может улучшить
    такой сигнал — выдаёт constant_q. LightGBM с full feature set
    (zone, edge_pct, category one-hots, side, horizon, evidence-fields)
    различает signals и даёт реальный probability.

    D41 pilot: LGB Brier 0.225 vs Beta 0.249 (+9.5%), AUC 0.677 vs 0.5.

Контракт:
    cal = LgbCalibrator()
    cal.fit(features_list, outcome_list)   # features_list: list[dict[str, float]]
    p = cal.transform(features_dict)        # один float
    ps = cal.transform([fd1, fd2, ...])     # np.ndarray
    payload = cal.serialize()                # для записи в ml_calibrators_lgb.model_payload
    cal2 = LgbCalibrator.deserialize(payload)

Inference fallback chain (в app/calibration/fallback_chain.py):
    LGB (если ml_calibrators_lgb.is_active=true для detector)
      → Beta (ml_calibrators)
      → raw_p

Edge cases:
- Если ВСЕ outcomes одинаковы (degenerate) — возвращаем constant_q как Beta
- N < min_samples (default 100 для LGB, выше чем Beta 30) → raise ValueError
- transform с missing feature → 0.0 (LightGBM умеет с missing)
"""

from __future__ import annotations

import base64
from collections.abc import Sequence
from dataclasses import dataclass

import numpy as np

DEFAULT_MIN_SAMPLES = 100  # LGB нужно больше данных чем Beta
DEFAULT_PARAMS = {
    "objective": "binary",
    "metric": "binary_logloss",
    "learning_rate": 0.05,
    "num_leaves": 15,
    "min_data_in_leaf": 30,
    "feature_fraction": 0.8,
    "bagging_fraction": 0.8,
    "bagging_freq": 5,
    "verbose": -1,
    "seed": 42,
}
DEFAULT_NUM_BOOST_ROUND = 500
DEFAULT_EARLY_STOPPING_ROUNDS = 25


@dataclass(frozen=True)
class LgbParams:
    """Параметры обученного LGB calibrator.

    model_b64 — LightGBM Booster.model_to_string(), затем base64 для JSON-safety.
    feature_names — упорядоченный список фичей (важен порядок: predict опирается на него).
    """

    model_b64: str
    feature_names: tuple[str, ...]
    n_train: int
    best_iteration: int
    degenerate: bool = False
    constant_q: float | None = None


class LgbCalibrator:
    """Per-detector LightGBM calibrator. Совместим по форме с BetaCalibrator."""

    def __init__(
        self,
        min_samples: int = DEFAULT_MIN_SAMPLES,
        lgb_params: dict | None = None,
        num_boost_round: int = DEFAULT_NUM_BOOST_ROUND,
        early_stopping_rounds: int = DEFAULT_EARLY_STOPPING_ROUNDS,
    ) -> None:
        self.min_samples = min_samples
        self.lgb_params = dict(DEFAULT_PARAMS) if lgb_params is None else dict(lgb_params)
        self.num_boost_round = num_boost_round
        self.early_stopping_rounds = early_stopping_rounds
        self.params: LgbParams | None = None
        self._booster = None  # lazy-loaded lightgbm.Booster

    # ---------- fit ----------

    def fit(
        self,
        features: Sequence[dict[str, float]],
        outcome: Sequence[int],
        valid_features: Sequence[dict[str, float]] | None = None,
        valid_outcome: Sequence[int] | None = None,
    ) -> LgbCalibrator:
        """Обучает booster.

        features:        list of feature dicts (порядок ключей берётся из features[0])
        outcome:         list of int (1=WIN, 0=LOSS)
        valid_features:  optional holdout для early stopping. Если None — берём
                         последние 20% train chronologically (caller отвечает за порядок).
        """
        if len(features) != len(outcome):
            raise ValueError(f"len mismatch: features={len(features)} outcome={len(outcome)}")
        if len(features) < self.min_samples:
            raise ValueError(f"N={len(features)} < min_samples={self.min_samples}")
        if not features:
            raise ValueError("empty features")

        # Degenerate: все outcomes одинаковы → нечему учиться, fallback на constant.
        y_arr = np.asarray(outcome, dtype=int)
        if len(np.unique(y_arr)) < 2:
            constant_q = float(y_arr.mean())
            feature_names = tuple(sorted(features[0].keys()))
            self.params = LgbParams(
                model_b64="",
                feature_names=feature_names,
                n_train=len(features),
                best_iteration=0,
                degenerate=True,
                constant_q=constant_q,
            )
            return self

        try:
            import lightgbm as lgb
        except ImportError as exc:
            raise RuntimeError("lightgbm not installed — pip install lightgbm") from exc

        feature_names = tuple(sorted(features[0].keys()))
        X = np.array(
            [[float(f.get(k, 0.0) or 0.0) for k in feature_names] for f in features],
            dtype=np.float64,
        )

        # Holdout для early stopping
        if valid_features is not None and valid_outcome is not None:
            if len(valid_features) != len(valid_outcome):
                raise ValueError("valid_features/valid_outcome length mismatch")
            X_va = np.array(
                [[float(f.get(k, 0.0) or 0.0) for k in feature_names] for f in valid_features],
                dtype=np.float64,
            )
            y_va = np.asarray(valid_outcome, dtype=int)
            X_tr, y_tr = X, y_arr
        else:
            # Chronological 80/20 (caller отвечает за порядок!)
            cut = int(len(features) * 0.8)
            if cut < self.min_samples // 2 or len(features) - cut < 10:
                # Слишком мало — fit на всём, no early stopping
                X_tr, y_tr = X, y_arr
                X_va, y_va = X, y_arr
            else:
                X_tr, y_tr = X[:cut], y_arr[:cut]
                X_va, y_va = X[cut:], y_arr[cut:]

        train_set = lgb.Dataset(X_tr, label=y_tr, feature_name=list(feature_names))
        valid_set = lgb.Dataset(X_va, label=y_va, feature_name=list(feature_names), reference=train_set)

        booster = lgb.train(
            self.lgb_params,
            train_set,
            num_boost_round=self.num_boost_round,
            valid_sets=[valid_set],
            callbacks=[lgb.early_stopping(stopping_rounds=self.early_stopping_rounds, verbose=False)],
        )

        model_str = booster.model_to_string()
        model_b64 = base64.b64encode(model_str.encode("utf-8")).decode("ascii")
        self.params = LgbParams(
            model_b64=model_b64,
            feature_names=feature_names,
            n_train=len(features),
            best_iteration=int(booster.best_iteration or self.num_boost_round),
        )
        self._booster = booster
        return self

    # ---------- inference ----------

    def _ensure_booster(self):
        if self._booster is not None:
            return self._booster
        if self.params is None or self.params.degenerate:
            return None
        try:
            import lightgbm as lgb
        except ImportError as exc:
            raise RuntimeError("lightgbm not installed") from exc
        model_str = base64.b64decode(self.params.model_b64.encode("ascii")).decode("utf-8")
        self._booster = lgb.Booster(model_str=model_str)
        return self._booster

    def transform(
        self,
        features: dict[str, float] | Sequence[dict[str, float]],
    ) -> float | np.ndarray:
        """Predict probability(WIN) для одного dict или массива dicts."""
        if self.params is None:
            raise RuntimeError("LgbCalibrator not fitted")

        scalar = isinstance(features, dict)
        feats = [features] if scalar else list(features)

        if self.params.degenerate:
            const = self.params.constant_q if self.params.constant_q is not None else 0.5
            arr = np.full(len(feats), const, dtype=np.float64)
            return float(arr[0]) if scalar else arr

        booster = self._ensure_booster()
        if booster is None:
            const = self.params.constant_q if self.params.constant_q is not None else 0.5
            arr = np.full(len(feats), const, dtype=np.float64)
            return float(arr[0]) if scalar else arr

        fn = self.params.feature_names
        X = np.array(
            [[float(f.get(k, 0.0) or 0.0) for k in fn] for f in feats],
            dtype=np.float64,
        )
        preds = np.asarray(booster.predict(X, num_iteration=self.params.best_iteration or None), dtype=np.float64)
        # Clip к [eps, 1-eps] чтобы не давать 0 или 1 для downstream EV-sizer
        preds = np.clip(preds, 1e-6, 1.0 - 1e-6)
        return float(preds[0]) if scalar else preds

    def brier(
        self,
        features: Sequence[dict[str, float]],
        outcome: Sequence[int],
    ) -> float:
        preds = self.transform(features)
        y = np.asarray(outcome, dtype=float)
        if isinstance(preds, float):
            return float((preds - y[0]) ** 2)
        return float(np.mean((preds - y) ** 2))

    # ---------- serde ----------

    def serialize(self) -> dict:
        if self.params is None:
            raise RuntimeError("LgbCalibrator not fitted")
        return {
            "model_b64": self.params.model_b64,
            "feature_names": list(self.params.feature_names),
            "n_train": self.params.n_train,
            "best_iteration": self.params.best_iteration,
            "degenerate": self.params.degenerate,
            "constant_q": self.params.constant_q,
        }

    @classmethod
    def deserialize(cls, data: dict) -> LgbCalibrator:
        cal = cls()
        cal.params = LgbParams(
            model_b64=str(data.get("model_b64") or ""),
            feature_names=tuple(data.get("feature_names") or ()),
            n_train=int(data.get("n_train") or 0),
            best_iteration=int(data.get("best_iteration") or 0),
            degenerate=bool(data.get("degenerate", False)),
            constant_q=(float(data["constant_q"]) if data.get("constant_q") is not None else None),
        )
        return cal
