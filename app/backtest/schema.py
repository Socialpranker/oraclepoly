"""Backtest dataclasses — pure data, no logic.

См. docs/superpowers/plans/2026-05-backtest-framework/SPEC.md
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from enum import StrEnum
from pathlib import Path
from typing import Any

DEFAULT_CANONICAL_PATH = Path("/mnt/data/canonical")


class FeeMode(StrEnum):
    """Fee model для backtest P&L.

    flat: единая константа ROUND_TRIP_FEE_PCT_FLAT (3.0%) для всех сигналов.
        Совместимость со старыми результатами. Систематически завышает fees
        в extreme zones (p×(1-p) → 0).
    dynamic: per-signal через fee_calculator (taker fee × 2 sides).
        Honest mathematics. На D44 entries @ 0.97 Crypto: ~0.42% round-trip.
    """

    FLAT = "flat"
    DYNAMIC = "dynamic"


class SlippageMode(StrEnum):
    """Slippage model для backtest entry price.

    none: нет slippage haircut. Theoretical mid execution.
    baseline: фикс 2% от cost_quoted (per-asset). Conservative.
    realistic: per-asset с volume-based depth proxy через apply_realistic_slippage.
        Use lifetime_volume_usd / horizon_days как daily volume proxy.
    """

    NONE = "none"
    BASELINE = "baseline"
    REALISTIC = "realistic"


class EmitMode(StrEnum):
    """Когда replay engine считает сигнал.

    first: один SignalOutcome на market — первый tick где detector эмитит.
        Default до 2026-05. Создаёт survivorship bias к winners потому что
        markets с band-exit-re-entry эмитятся только в первый момент входа.
    per_tick: каждый tick где detector эмитит = новый SignalOutcome.
        Соответствует Wilson на per-emission. Overcounts на correlated ticks.
    cron_interval: эмулирует prod cron sampling — эмит каждые N минут пока
        detector в band. Closest to live emission density.
    """

    FIRST = "first"
    PER_TICK = "per_tick"
    CRON_INTERVAL = "cron_interval"


@dataclass(frozen=True)
class BacktestConfig:
    """Конфигурация backtest run.

    Frozen — после создания не меняется (replay deterministic).
    """

    detector_name: str  # 'd44', 'd45', etc — lowercase
    start_date: datetime  # tz-aware UTC
    end_date: datetime  # tz-aware UTC, exclusive
    canonical_path: Path = DEFAULT_CANONICAL_PATH
    category_filter: frozenset[str] | None = None
    max_markets: int | None = None  # для quick iteration
    tick_stride: int = 1  # каждый N-й snapshot (1 = all)

    # Honest-execution knobs (added 2026-05-24).
    # Дефолты honest с 2026-06-24 (post-mortem backtest→prod): старые дефолты
    # FLAT/NONE/FIRST систематически вводили в заблуждение (flat fee завышает на
    # extreme-entry, none slippage занижает издержки, first-emit = survivorship
    # bias). См. research/backtest-prod-gap-postmortem-2026-06-24/.
    fee_mode: FeeMode = FeeMode.DYNAMIC
    slippage_mode: SlippageMode = SlippageMode.REALISTIC
    emit_mode: EmitMode = EmitMode.CRON_INTERVAL
    stake_usd_per_signal: float = 100.0  # для slippage utilization
    cron_interval_minutes: int = 5  # для EmitMode.CRON_INTERVAL


@dataclass(frozen=True)
class SignalOutcome:
    """Один эмитированный signal с известным outcome.

    `won` вычисляется при создании: side='YES' & yes_won=1 OR side='NO' & yes_won=0.
    """

    market_id: str  # condition_id
    event_id: str | None
    category: str
    side: str  # 'YES' | 'NO'
    entry_price: float  # side-aware: для NO = 1 - yes_price
    fair_price: float
    confidence: float
    expected_edge_pct: float
    hours_to_resolve: int  # на момент эмита
    yes_won: int  # 0 or 1
    won: bool
    question: str  # truncated для report


@dataclass(frozen=True)
class CategoryStats:
    """Per-category aggregation."""

    category: str
    n_signals: int
    n_wins: int
    win_rate: float
    wilson_lower_95: float
    wilson_upper_95: float
    avg_edge_pct: float
    gross_roi_pct: float = 0.0  # Avg ROI per signal (gross, без fees)
    net_roi_pct: float = 0.0  # После 3% fee drag (Polymarket sports = 1.5% × 2 sides)


@dataclass(frozen=True)
class BacktestResult:
    """Final result of backtest run."""

    config: BacktestConfig
    n_signals_emitted: int
    n_unique_markets: int
    n_unique_events: int
    n_wins: int
    win_rate: float
    wilson_lower_95: float
    wilson_upper_95: float
    avg_edge_pct: float
    gross_roi_pct: float = 0.0  # Sum(per-signal ROI) / N — gross
    net_roi_pct: float = 0.0  # После 3% fee drag (Polymarket round-trip)
    per_category: dict[str, CategoryStats] = field(default_factory=dict)
    top_losses: list[SignalOutcome] = field(default_factory=list)  # by confidence
    elapsed_sec: float = 0.0
    metadata: dict[str, Any] = field(default_factory=dict)  # data_sources_loaded, n_rows, etc
