"""Pre-filter mirror — replicates prod SQL pre-filters from dN_scan.py.

Каждый production cron scan (app/jobs/dN_scan.py) делает SQL pre-filter ДО
вызова detect(). Backtest harness без этого видит markets, которые prod
никогда бы не торговал — overcounts density (D7b: 4376 sig в backtest vs
~1260 в prod, 3.5× over).

Этот module зеркалит SQL pre-filters в Python — adapter применяет PreFilter
к каждому (market, snapshot) ДО detect() в run_backtest. SSoT — `app/jobs/dN_scan.py`.

См. PR #498 follow-up (research/d7b-autoresearch-2026-05/gap_findings.md).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass(frozen=True)
class PreFilter:
    """Mirror of SQL pre-filter from prod scan.

    Все ranges — inclusive low, exclusive high (как SQL `>= AND <`).
    None в categories_include = не ограничивать. Empty exclude = no exclusions.
    None в neg_risk_required = не ограничивать.

    `extra` — детектор-специфичные предикаты (n trades, event size etc.).
    Возвращают bool. Применяются после standard filters.
    """

    current_price_min: float = 0.0
    current_price_max: float = 1.0
    hours_to_resolve_min: float = 0.0
    hours_to_resolve_max: float = 99999.0
    categories_include: frozenset[str] | None = None
    categories_exclude: frozenset[str] = field(default_factory=frozenset)
    volume_24h_min: float = 0.0
    volume_24h_max: float = float("inf")
    neg_risk_required: bool | None = None

    def passes(self, market_event: dict[str, Any]) -> bool:
        """True если market_event проходит pre-filter."""
        price = float(market_event.get("current_price") or 0.0)
        if not (self.current_price_min <= price < self.current_price_max):
            return False

        htr = float(market_event.get("hours_to_resolve") or 0.0)
        if not (self.hours_to_resolve_min <= htr < self.hours_to_resolve_max):
            return False

        category = str(market_event.get("category") or "")
        if self.categories_include is not None and category not in self.categories_include:
            return False
        if category in self.categories_exclude:
            return False

        vol_24h = float(market_event.get("volume_24h") or 0.0)
        if not (self.volume_24h_min <= vol_24h <= self.volume_24h_max):
            return False

        if self.neg_risk_required is not None:
            neg_risk = bool(market_event.get("neg_risk", False))
            if neg_risk != self.neg_risk_required:
                return False

        return True


# Per-detector registry. SSoT — соответствующий dN_scan.py файл.
# При изменении SQL prod scan'а нужно синхронно обновить PreFilter здесь —
# иначе backtest density уйдёт от prod.
_PRE_FILTERS: dict[str, PreFilter] = {
    # D7b: app/jobs/d7b_scan.py, current_price ∈ [0.85, 0.97), DTE [4, 72)h,
    # exclude Culture, volume_24h ≥ 5000
    "d7b": PreFilter(
        current_price_min=0.85,
        current_price_max=0.97,
        hours_to_resolve_min=4.0,
        hours_to_resolve_max=72.0,
        categories_exclude=frozenset({"Culture"}),
        volume_24h_min=5000.0,
    ),
    # D5: app/jobs/d5_scan.py, [0.70, 0.99), DTE [72, 720)h, exclude Other + Minute
    "d5": PreFilter(
        current_price_min=0.70,
        current_price_max=0.99,
        hours_to_resolve_min=72.0,
        hours_to_resolve_max=720.0,
        categories_exclude=frozenset({"Other", "Minute Markets"}),
    ),
    # D31: app/jobs/d31_scan.py, [0.05, 0.20), DTE [72, 720)h,
    # include Sports/Other, volume_24h ∈ [100, 5000]
    "d31": PreFilter(
        current_price_min=0.05,
        current_price_max=0.20,
        hours_to_resolve_min=72.0,
        hours_to_resolve_max=720.0,
        categories_include=frozenset({"Sports", "Other"}),
        volume_24h_min=100.0,
        volume_24h_max=5000.0,
    ),
    # D32: app/jobs/d32_scan.py, [0.85, 0.95), DTE [72, 720)h,
    # include Sports/Other/Politics/Tech & AI, volume_24h ∈ [100, 5000]
    "d32": PreFilter(
        current_price_min=0.85,
        current_price_max=0.95,
        hours_to_resolve_min=72.0,
        hours_to_resolve_max=720.0,
        categories_include=frozenset({"Sports", "Other", "Politics", "Tech & AI"}),
        volume_24h_min=100.0,
        volume_24h_max=5000.0,
    ),
    # D33: app/jobs/d33_scan.py, [0.80, 0.95), DTE [48, 168)h,
    # include Politics/Other/Tech/Sports, neg_risk=false, volume_24h ∈ [100, 5000]
    "d33": PreFilter(
        current_price_min=0.80,
        current_price_max=0.95,
        hours_to_resolve_min=48.0,
        hours_to_resolve_max=168.0,
        categories_include=frozenset({"Politics", "Other", "Tech & AI", "Sports"}),
        volume_24h_min=100.0,
        volume_24h_max=5000.0,
        neg_risk_required=False,
    ),
    # D44: detector сам делает все эти checks внутри. Пустой filter — no-op
    # для backwards compat. Detector останется единственным gating mechanism.
    "d44": PreFilter(),
}


def get_pre_filter(detector_name: str) -> PreFilter:
    """Lookup pre-filter by detector name. Unknown detector → no-op PreFilter."""
    return _PRE_FILTERS.get(detector_name.lower(), PreFilter())


__all__ = ["PreFilter", "get_pre_filter"]
