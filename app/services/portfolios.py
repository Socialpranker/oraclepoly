"""Portfolio resolver + gate-override engine.

Phase 2 of portfolios refactor (план bright-hopping-meteor.md).

Один portfolio = bankroll + exposure cap + kelly fraction + gate overrides.
Каждый detector принадлежит ровно одному portfolio через
`detector_state.portfolio_id` FK (Phase 1 schema).

Эта модуль — pure service layer:
- `fetch_portfolio(id)` — один portfolio по ID
- `fetch_all_portfolios()` — все portfolios
- `fetch_portfolio_for_detector(detector)` — portfolio assigned к detector
- `apply_gate_overrides(gates, overrides)` — pure function для модификации gates dict

Wiring в v2_execution + endpoints — следующие фазы (PR-3+).
"""

from __future__ import annotations

import copy
import json
import time
from dataclasses import dataclass, field
from typing import Any

from app.services.database import fetch, fetchrow

# ─── Data model ──────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class Portfolio:
    portfolio_id: str
    display_name: str
    bankroll_usd: float
    exposure_cap_usd: float
    kelly_fraction: float
    gate_overrides: dict[str, str] = field(default_factory=dict)
    enabled: bool = True


def _row_to_portfolio(row: dict[str, Any]) -> Portfolio:
    """Convert asyncpg Record/dict в Portfolio dataclass.

    `gate_overrides` приходит из jsonb как dict (asyncpg auto-decoding),
    но если это str (manual JSON) — парсим явно.
    """
    raw_overrides = row["gate_overrides"]
    if isinstance(raw_overrides, str):
        overrides = json.loads(raw_overrides) if raw_overrides else {}
    elif raw_overrides is None:
        overrides = {}
    else:
        overrides = dict(raw_overrides)
    return Portfolio(
        portfolio_id=row["portfolio_id"],
        display_name=row["display_name"],
        bankroll_usd=float(row["bankroll_usd"]),
        exposure_cap_usd=float(row["exposure_cap_usd"]),
        kelly_fraction=float(row["kelly_fraction"]),
        gate_overrides=overrides,
        enabled=bool(row["enabled"]),
    )


# ─── Process-local TTL cache (60s) ───────────────────────────────────────────
# Portfolios — config-like (меняются редко, через CLI Phase 6).
# Cache избегает DB round-trip per signal в v2_execution.

_CACHE_TTL_SEC = 60.0
_cache_all: tuple[float, list[Portfolio]] | None = None
_cache_by_detector: dict[str, tuple[float, Portfolio | None]] = {}


def _cache_fresh(ts: float) -> bool:
    return (time.monotonic() - ts) < _CACHE_TTL_SEC


def invalidate_cache() -> None:
    """Очистить кэш — используется в тестах + после CLI assignment (Phase 6)."""
    global _cache_all
    _cache_all = None
    _cache_by_detector.clear()


# ─── Fetch ───────────────────────────────────────────────────────────────────


_COLUMNS = "portfolio_id, display_name, bankroll_usd, exposure_cap_usd, kelly_fraction, gate_overrides, enabled"


async def fetch_portfolio(portfolio_id: str) -> Portfolio | None:
    """Один portfolio по ID. Returns None если не найден."""
    row = await fetchrow(
        f"SELECT {_COLUMNS} FROM portfolios WHERE portfolio_id = $1",
        portfolio_id,
    )
    return _row_to_portfolio(dict(row)) if row else None


async def fetch_all_portfolios() -> list[Portfolio]:
    """Все portfolios, отсортированы по portfolio_id. Кэшируется на 60s."""
    global _cache_all
    if _cache_all is not None and _cache_fresh(_cache_all[0]):
        return _cache_all[1]
    rows = await fetch(f"SELECT {_COLUMNS} FROM portfolios ORDER BY portfolio_id")
    portfolios = [_row_to_portfolio(dict(r)) for r in rows]
    _cache_all = (time.monotonic(), portfolios)
    return portfolios


async def fetch_portfolio_for_detector(detector: str) -> Portfolio | None:
    """Portfolio assigned к detector через detector_state.portfolio_id JOIN.

    Returns None если detector не зарегистрирован OR portfolio_id is null
    (это invariant violation после Phase 1 — Phase 7 startup check ловит).
    Кэшируется per-detector на 60s.
    """
    cached = _cache_by_detector.get(detector)
    if cached is not None and _cache_fresh(cached[0]):
        return cached[1]
    row = await fetchrow(
        """
        SELECT p.portfolio_id, p.display_name, p.bankroll_usd, p.exposure_cap_usd,
               p.kelly_fraction, p.gate_overrides, p.enabled
          FROM detector_state ds
          JOIN portfolios p ON p.portfolio_id = ds.portfolio_id
         WHERE ds.detector = $1
        """,
        detector,
    )
    portfolio = _row_to_portfolio(dict(row)) if row else None
    _cache_by_detector[detector] = (time.monotonic(), portfolio)
    return portfolio


async def fetch_portfolio_map() -> dict[str, str]:
    """Return {detector: portfolio_id} для всех зарегистрированных detectors.

    Используется endpoints для batch lookup без N+1 queries.
    """
    rows = await fetch("SELECT detector, portfolio_id FROM detector_state WHERE portfolio_id IS NOT NULL")
    return {r["detector"]: r["portfolio_id"] for r in rows}


# ─── Gate override engine ────────────────────────────────────────────────────

# Recognised override actions
_OVERRIDE_SKIP = "skip"
_OVERRIDE_RELAX = "relax"  # caller-handled (gate-specific threshold lowering)
_RECOGNISED_ACTIONS = frozenset({_OVERRIDE_SKIP, _OVERRIDE_RELAX})


def apply_gate_overrides(
    gates: dict[str, dict[str, Any]],
    overrides: dict[str, str],
) -> tuple[dict[str, dict[str, Any]], list[str]]:
    """Apply portfolio gate overrides to a gates dict (immutable copy).

    Args:
        gates: Mapping {gate_name: {passed, value, label, ...}}.
        overrides: Mapping {gate_name: "skip"|"relax"}.

    Returns:
        Tuple of (mutated_gates_copy, applied_gate_names).
        - "skip": gate marked passed=True, override='skip' annotation added.
        - "relax": annotation only — caller handles threshold change.
        - Unknown gate_name OR unknown action: no-op.

    Pure function: input `gates` dict is never mutated.
    """
    if not overrides:
        return copy.deepcopy(gates), []

    new_gates = copy.deepcopy(gates)
    applied: list[str] = []
    for gate_name, action in overrides.items():
        if gate_name not in new_gates:
            continue  # unknown gate — no-op
        if action not in _RECOGNISED_ACTIONS:
            continue  # unknown action — no-op
        gate = new_gates[gate_name]
        if action == _OVERRIDE_SKIP:
            gate["passed"] = True
            gate["override"] = _OVERRIDE_SKIP
        elif action == _OVERRIDE_RELAX:
            gate["override"] = _OVERRIDE_RELAX
        applied.append(gate_name)
    return new_gates, applied
