"""Stage 2: Strict gate — n, ROI, mirror, year-split, FDR.

For each cell from grid_generator, query the canonical parquet and compute
metrics. Reject if any of:
  - n < MIN_N or n_unique_markets < MIN_N_UNIQUE_MARKETS
  - net_ROI < MIN_NET_ROI
  - mirror_diff_pp < MIN_MIRROR_DIFF_PP (asymmetric edge required)
  - any year (2025/2026) net_ROI < 0 (sustained edge required)

Apply Benjamini-Hochberg FDR correction at α=0.05 to surviving cells.
"""

from __future__ import annotations

import logging
import math
from dataclasses import dataclass
from typing import Any

import duckdb
from scipy import stats as scipy_stats

from .config import (
    BACKTEST_DAYS,
    DEFAULT_AVG_STAKE_USD,
    DEFAULT_FEE_PCT,
    FDR_ALPHA,
    MIN_MIRROR_DIFF_PP,
    MIN_N,
    MIN_N_UNIQUE_MARKETS,
    MIN_NET_ROI,
    MIN_YEAR_SAMPLES,
)
from .economics import (
    compute_metrics,
    daily_pnl,
    fee_drag,
    slippage_pct,
)
from .grid_generator import Cell

logger = logging.getLogger(__name__)


@dataclass
class CellResult:
    """Result of evaluating one cell."""

    cell: Cell
    n_signals: int
    n_unique_markets: int
    win_rate: float
    avg_cost: float
    net_roi: float
    mirror_net_roi: float
    mirror_diff_pp: float
    year_2025_net_roi: float | None
    year_2026_net_roi: float | None
    year_consistency: float  # 0..1
    daily_signals: float
    daily_raw_pnl: float
    daily_honest_pnl: float
    p_value: float
    bh_adjusted_p: float | None  # filled in by apply_fdr()
    verdict: str  # 'PASS' | 'MARGINAL' | 'FAIL'

    def __post_init__(self):
        if self.bh_adjusted_p is None:
            pass  # will be set by apply_fdr

    def to_dict(self) -> dict[str, Any]:
        return {
            "cell": self.cell,
            "n_signals": self.n_signals,
            "n_unique_markets": self.n_unique_markets,
            "win_rate": self.win_rate,
            "avg_cost": self.avg_cost,
            "net_roi": self.net_roi,
            "mirror_net_roi": self.mirror_net_roi,
            "mirror_diff_pp": self.mirror_diff_pp,
            "year_2025_net_roi": self.year_2025_net_roi,
            "year_2026_net_roi": self.year_2026_net_roi,
            "year_consistency": self.year_consistency,
            "daily_signals": self.daily_signals,
            "daily_raw_pnl": self.daily_raw_pnl,
            "daily_honest_pnl": self.daily_honest_pnl,
            "p_value": self.p_value,
            "bh_adjusted_p": self.bh_adjusted_p,
            "verdict": self.verdict,
        }


def _build_where_clause(cell: Cell) -> str:
    """Build SQL WHERE fragment for cell's filters (excludes anchor + zone — handled outside)."""
    parts = []
    parts.append(f"{cell.feature} IS NOT NULL")
    parts.append(f"{cell.feature} {cell.threshold_op} {cell.threshold_value}")
    if cell.category is not None:
        # Escape single quotes in category names
        safe_cat = cell.category.replace("'", "''")
        parts.append(f"category = '{safe_cat}'")
    return " AND ".join(parts)


def evaluate_cell(
    con: duckdb.DuckDBPyConnection,
    cell: Cell,
    parquet_path: str,
    fee_pct: float = DEFAULT_FEE_PCT,
    backtest_days: int = BACKTEST_DAYS,
) -> CellResult | None:
    """Run one cell's gate. Return CellResult on PASS/MARGINAL, None on FAIL."""
    where = _build_where_clause(cell)
    sql = f"""
        SELECT yes_won, yes_price_at_anchor, condition_id, resolve_year
        FROM read_parquet('{parquet_path}')
        WHERE anchor_hours = {cell.anchor}
          AND yes_won IS NOT NULL
          AND yes_price_at_anchor >= {cell.zone_lo}
          AND yes_price_at_anchor < {cell.zone_hi}
          AND {where}
    """
    try:
        df = con.execute(sql).fetchdf()
    except Exception as exc:
        logger.warning(
            "gate_query_failed",
            extra={"cell_signature": cell.signature(), "error": str(exc)},
        )
        return None

    n = len(df)
    if n < MIN_N:
        return None

    n_unique = int(df["condition_id"].nunique())
    if n_unique < MIN_N_UNIQUE_MARKETS:
        return None

    metrics = compute_metrics(df, cell.side, cell.zone, fee_pct)
    if metrics["net_roi"] < MIN_NET_ROI:
        return None

    # Mirror — flip side, same universe
    mirror_side = "YES" if cell.side == "NO" else "NO"
    mirror = compute_metrics(df, mirror_side, cell.zone, fee_pct)
    mirror_diff_pp = (metrics["net_roi"] - mirror["net_roi"]) * 100
    if mirror_diff_pp < MIN_MIRROR_DIFF_PP:
        return None

    # Year split
    year_results = {}
    for y in (2025, 2026):
        sub = df[df["resolve_year"] == y]
        if len(sub) >= MIN_YEAR_SAMPLES:
            ym = compute_metrics(sub, cell.side, cell.zone, fee_pct)
            year_results[y] = ym["net_roi"]

    if year_results:
        if any(v < 0.0 for v in year_results.values()):
            return None
        if len(year_results) >= 2:
            consistency = min(year_results.values()) / max(year_results.values())
        else:
            consistency = 0.5
    else:
        consistency = 0.5

    # p-value: one-sided binomial vs WR breakeven
    drag = fee_drag(fee_pct)
    slip = slippage_pct(cell.zone)
    cost_net = metrics["avg_cost"] + slip
    if cost_net > 0 and (1 - drag) > 0:
        p_break = cost_net / (1 - drag)
        p_break = max(0.001, min(0.999, p_break))
        successes = int(metrics["wr"] * n)
        try:
            p_value = float(scipy_stats.binomtest(successes, n, p=p_break, alternative="greater").pvalue)
        except Exception:
            p_value = 1.0
    else:
        p_value = 1.0

    daily_signals_val = n / backtest_days if backtest_days > 0 else 0.0
    daily_raw = daily_pnl(n, metrics["net_roi"], backtest_days, DEFAULT_AVG_STAKE_USD, honest=False)
    daily_honest = daily_pnl(n, metrics["net_roi"], backtest_days, DEFAULT_AVG_STAKE_USD, honest=True)

    verdict = "PASS" if metrics["net_roi"] >= 0.10 else "MARGINAL"

    return CellResult(
        cell=cell,
        n_signals=n,
        n_unique_markets=n_unique,
        win_rate=metrics["wr"],
        avg_cost=metrics["avg_cost"],
        net_roi=metrics["net_roi"],
        mirror_net_roi=mirror["net_roi"],
        mirror_diff_pp=mirror_diff_pp,
        year_2025_net_roi=year_results.get(2025),
        year_2026_net_roi=year_results.get(2026),
        year_consistency=consistency,
        daily_signals=daily_signals_val,
        daily_raw_pnl=daily_raw,
        daily_honest_pnl=daily_honest,
        p_value=p_value,
        bh_adjusted_p=None,  # set by apply_fdr
        verdict=verdict,
    )


def apply_fdr(results: list[CellResult], alpha: float = FDR_ALPHA) -> list[CellResult]:
    """Benjamini-Hochberg FDR correction.

    Sorts by p-value ascending. For rank i, threshold = (i/m) * alpha.
    Cells passing FDR have bh_adjusted_p set; those failing get penalty
    in rank_score but are NOT removed (manual review can pursue).

    Returns the input list with bh_adjusted_p populated on each result.
    """
    if not results:
        return results
    sorted_res = sorted(results, key=lambda r: r.p_value)
    m = len(sorted_res)
    for i, r in enumerate(sorted_res, start=1):
        # q-value = p * m / i (BH adjustment)
        q = r.p_value * m / i
        r.bh_adjusted_p = min(q, 1.0)
    return results


def rank_score(r: CellResult, overlap_max: float) -> float:
    """Composite score for ranking. See plan §5.

    Multiplicative — any zero kills the candidate.
    """
    n_factor = math.log10(max(r.n_signals, 100))  # 2..3.5
    roi_factor = max(r.net_roi, 0.0)  # 0..0.30+
    mirror_factor = min(r.mirror_diff_pp / 20.0, 2.0)  # 0..2 cap
    consistency = max(r.year_consistency, 0.0)  # 0..1
    novelty = max(0.0, 1.0 - overlap_max)  # 0..1
    fdr_q = r.bh_adjusted_p if r.bh_adjusted_p is not None else 1.0
    fdr_penalty = 1.0 if fdr_q < FDR_ALPHA else 0.5

    return (
        n_factor
        * roi_factor
        * (1.0 + mirror_factor)
        * (0.5 + 0.5 * consistency)
        * (0.3 + 0.7 * novelty)
        * fdr_penalty
    )
