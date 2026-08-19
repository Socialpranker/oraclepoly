"""FeatureVector and ScoringConfig — Pydantic models for feature_store rows and Oracle configs."""

from __future__ import annotations

import hashlib
import json
from datetime import datetime
from typing import Literal

from pydantic import BaseModel, Field, model_validator

ComponentName = Literal[
    "whale",
    "news",
    "maker",
    "taker",
    "sm_fresh",
    "sm_cluster",
    "sm_registry",
    "oi",
    "conc",
    "micro",
]


class FeatureVector(BaseModel):
    """One row of feature_store. Components with NULL raw values are excluded from components_available."""

    market_id: str
    condition_id: str | None = None
    event_id: str | None = None
    snapshot_at: datetime
    side: Literal["YES", "NO"]

    category: str | None = None
    question: str | None = None
    horizon_days: float | None = None

    market_price: float
    entry_price: float | None = None
    vwap_7d: float | None = None
    volume_24h: float | None = None

    maker_direction_raw: float | None = None
    taker_imbalance_raw: float | None = None
    whale_tx_count: int = 0

    whale_contribution_raw: float | None = None
    news_contribution_raw: float | None = None
    sm_fresh_raw: float | None = None
    sm_cluster_raw: float | None = None
    sm_registry_raw: float | None = None
    oi_factor_raw: float | None = None
    concentration_factor_raw: float | None = None
    micro_factor_raw: float | None = None

    outcome: Literal["yes", "no", "pending"] | None = None
    win: int | None = Field(default=None, ge=0, le=1)
    resolved_at: datetime | None = None

    source: Literal["hf", "live", "hf_enriched"]
    features_version: str = "1.0"

    components_available: set[ComponentName] = Field(default_factory=set)

    @model_validator(mode="after")
    def _derive_components(self) -> FeatureVector:
        """Populate components_available from which *_raw fields are non-NULL."""
        comp_map = {
            "whale": self.whale_contribution_raw,
            "news": self.news_contribution_raw,
            "maker": self.maker_direction_raw,
            "taker": self.taker_imbalance_raw,
            "sm_fresh": self.sm_fresh_raw,
            "sm_cluster": self.sm_cluster_raw,
            "sm_registry": self.sm_registry_raw,
            "oi": self.oi_factor_raw,
            "conc": self.concentration_factor_raw,
            "micro": self.micro_factor_raw,
        }
        derived = {name for name, val in comp_map.items() if val is not None}
        self.components_available = set(self.components_available) | derived
        return self


class ScoringConfig(BaseModel):
    """Versioned Oracle scoring parameters. Hash is deterministic over weights+flags+calibration."""

    label: str
    weights: dict[str, float]
    formula_flags: dict[str, str] = Field(default_factory=dict)
    calibration_source: str = "global"
    notes: str | None = None

    @property
    def hash(self) -> str:
        """Deterministic 16-char md5 over sorted JSON of (weights, flags, calibration)."""
        canonical = json.dumps(
            {
                "w": dict(sorted(self.weights.items())),
                "f": dict(sorted(self.formula_flags.items())),
                "c": self.calibration_source,
            },
            sort_keys=True,
            separators=(",", ":"),
        )
        return hashlib.md5(canonical.encode("utf-8")).hexdigest()[:16]
