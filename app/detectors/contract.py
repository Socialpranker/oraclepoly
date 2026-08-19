"""Signal contract — унифицированный формат вывода всех детекторов.

Все детекторы эмитят `Signal` с одинаковыми полями. Strategy engine и ML calibrator
работают с этим контрактом, не зависят от конкретного детектора.
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from typing import Any, Literal

Side = Literal["YES", "NO"]


@dataclass(frozen=True)
class Signal:
    """Один торговый сигнал от детектора.

    Иммутабельный (frozen=True) — после эмита нельзя править.
    Сериализуется в JSON через `to_dict()` / `from_dict()` для Redis pub/sub.
    """

    # Identity
    detector: str  # 'D1_order_flow', 'D3_cross_mkt_arb', etc
    market_id: str
    side: Side  # YES | NO
    generated_at: datetime  # tz-aware, UTC

    # Core
    confidence: float  # [0, 1] — насколько уверен детектор
    expected_edge_pct: float  # ROI vs market_price, в процентах
    fair_price: float  # 0..1 — model думает price должна быть тут
    current_price: float  # 0..1 — реальная market_price на момент сигнала

    # TTL
    horizon_sec: int  # сколько живёт сигнал (после — stale)

    # Optional
    event_id: str | None = None  # для cross-market features
    token_id: str | None = None  # CLOB token (если знаем)
    evidence: dict[str, Any] = field(default_factory=dict)  # debug / interpretability

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        d["generated_at"] = self.generated_at.isoformat()
        return d

    def to_json(self) -> str:
        return json.dumps(self.to_dict())

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> Signal:
        if isinstance(data.get("generated_at"), str):
            data = {**data, "generated_at": datetime.fromisoformat(data["generated_at"])}
        return cls(**data)

    @classmethod
    def from_json(cls, raw: str) -> Signal:
        return cls.from_dict(json.loads(raw))

    @property
    def divergence(self) -> float:
        """Signed price diff: fair − current (positive = price too low for YES)."""
        return self.fair_price - self.current_price

    @property
    def is_stale(self) -> bool:
        """Сигнал устарел (now > generated_at + horizon)?"""
        return (datetime.now(UTC) - self.generated_at).total_seconds() > self.horizon_sec
