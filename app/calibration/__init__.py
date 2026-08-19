"""Calibration layer для детекторов.

L1 BetaCalibrator (Kull 2017) — per-detector, конвертирует raw confidence из
бэктеста в well-calibrated probability.

L2 LightGBM meta-labeler — глобальный, поверх L1; учит non-linear interactions
(detector overlap, regime shifts), выдаёт final P[trade ROI > 0] для EV-aware
sizing в execution.
"""

from app.calibration.beta_calibrator import BetaCalibrator
from app.calibration.ev_sizer import (
    SizingDecision,
    size_from_calibrated_p,
    size_from_raw_confidence,
)
from app.calibration.meta_labeler import (
    MetaLabelerConfig,
    MetaLabelerExample,
    TrainedMetaLabeler,
    deserialize_from_json,
    serialize_to_json,
    train_meta_labeler,
)

__all__ = [
    "BetaCalibrator",
    "MetaLabelerConfig",
    "MetaLabelerExample",
    "SizingDecision",
    "TrainedMetaLabeler",
    "deserialize_from_json",
    "serialize_to_json",
    "size_from_calibrated_p",
    "size_from_raw_confidence",
    "train_meta_labeler",
]
