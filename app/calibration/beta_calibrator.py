"""Beta calibration (Kull, Filho, Flach 2017).

Калибрует raw probability `p` из детектора → well-calibrated `q`.

Формула:
    logit(q) = a*log(p) + b*log(1-p) + c

То есть на parametric level это logistic regression на двух фичах
(log(p), log(1-p)) с интерсептом. Доминирует Platt scaling эмпирически
для skewed score distributions (Kull 2017, AISTATS).

Использование:
    cal = BetaCalibrator()
    cal.fit(raw_p=[0.7, 0.6, ...], outcome=[1, 0, ...])
    q = cal.transform(0.7)               # одно значение
    qs = cal.transform([0.7, 0.6, 0.5])  # массив
    params_dict = cal.serialize()        # для записи в БД
    cal2 = BetaCalibrator.deserialize(params_dict)

Edge cases:
- Если ВСЕ outcomes одинаковы (все WIN или все LOSS) — fit падает на
  LogisticRegression. Возвращаем degenerate calibrator (q = const = mean).
- Если raw_p == 0 или 1 — clip до [EPS, 1-EPS] чтобы log не взорвался.
- При N < min_samples (default 30) — raise ValueError, caller должен
  использовать fallback (shared prior).
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass

import numpy as np
from sklearn.linear_model import LogisticRegression

EPS = 1e-6
DEFAULT_MIN_SAMPLES = 30


@dataclass(frozen=True)
class BetaParams:
    """Параметры обученного Beta calibrator."""

    a: float
    b: float
    c: float
    degenerate: bool = False
    constant_q: float | None = None


class BetaCalibrator:
    """Beta calibration (Kull 2017): logit(q) = a*log(p) + b*log(1-p) + c."""

    def __init__(self, min_samples: int = DEFAULT_MIN_SAMPLES) -> None:
        self.min_samples = min_samples
        self.params: BetaParams | None = None

    @staticmethod
    def _features(p: np.ndarray) -> np.ndarray:
        clipped = np.clip(p, EPS, 1.0 - EPS)
        log_p = np.log(clipped)
        log_1mp = np.log(1.0 - clipped)
        return np.column_stack([log_p, log_1mp])

    def fit(self, raw_p: Sequence[float], outcome: Sequence[int]) -> BetaCalibrator:
        """Обучает калибратор на парах (raw_p, outcome).

        outcome: 1 = WIN, 0 = LOSS. Не передавать EXPIRED — те выкидываются caller'ом.
        """
        if len(raw_p) != len(outcome):
            raise ValueError(f"len mismatch: raw_p={len(raw_p)} outcome={len(outcome)}")
        if len(raw_p) < self.min_samples:
            raise ValueError(f"N={len(raw_p)} < min_samples={self.min_samples}")

        p_arr = np.asarray(raw_p, dtype=float)
        y_arr = np.asarray(outcome, dtype=int)

        if len(np.unique(y_arr)) < 2:
            constant_q = float(y_arr.mean())
            self.params = BetaParams(a=0.0, b=0.0, c=0.0, degenerate=True, constant_q=constant_q)
            return self

        x = self._features(p_arr)
        clf = LogisticRegression(C=1.0, solver="lbfgs", max_iter=1000)
        clf.fit(x, y_arr)
        a, b = float(clf.coef_[0][0]), float(clf.coef_[0][1])
        c = float(clf.intercept_[0])
        self.params = BetaParams(a=a, b=b, c=c)
        return self

    def transform(self, raw_p: float | Sequence[float]) -> float | np.ndarray:
        """Применяет обученный калибратор."""
        if self.params is None:
            raise RuntimeError("BetaCalibrator not fitted")
        scalar = isinstance(raw_p, int | float)
        p_arr = np.asarray([raw_p] if scalar else raw_p, dtype=float)

        if self.params.degenerate:
            const = self.params.constant_q if self.params.constant_q is not None else 0.5
            q = np.full_like(p_arr, const)
            return float(q[0]) if scalar else q

        x = self._features(p_arr)
        logit = self.params.a * x[:, 0] + self.params.b * x[:, 1] + self.params.c
        q = 1.0 / (1.0 + np.exp(-logit))
        return float(q[0]) if scalar else q

    def brier(self, raw_p: Sequence[float], outcome: Sequence[int]) -> float:
        """Brier score = mean((q - y)**2)."""
        q = self.transform(raw_p)
        y = np.asarray(outcome, dtype=float)
        if isinstance(q, float):
            return float((q - y[0]) ** 2)
        return float(np.mean((q - y) ** 2))

    def serialize(self) -> dict:
        if self.params is None:
            raise RuntimeError("BetaCalibrator not fitted")
        return {
            "a": self.params.a,
            "b": self.params.b,
            "c": self.params.c,
            "degenerate": self.params.degenerate,
            "constant_q": self.params.constant_q,
        }

    @classmethod
    def deserialize(cls, data: dict) -> BetaCalibrator:
        cal = cls()
        cal.params = BetaParams(
            a=float(data.get("a", 0.0) or 0.0),
            b=float(data.get("b", 0.0) or 0.0),
            c=float(data.get("c", 0.0) or 0.0),
            degenerate=bool(data.get("degenerate", False)),
            constant_q=(float(data["constant_q"]) if data.get("constant_q") is not None else None),
        )
        return cal


def raw_brier(raw_p: Sequence[float], outcome: Sequence[int]) -> float:
    """Brier score без калибровки (для сравнения 'до vs после')."""
    p = np.asarray(raw_p, dtype=float)
    y = np.asarray(outcome, dtype=float)
    return float(np.mean((p - y) ** 2))
