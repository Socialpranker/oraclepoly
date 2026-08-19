"""Stage 4: Persist — INSERT to auto_edge_discoveries + write MD report + CSV.

Avoids importing app.* (the scanner runs on host venv-research without
full app config). Uses asyncpg directly with DATABASE_URL env var.
"""

from __future__ import annotations

import logging
import os
import time
import uuid
from dataclasses import dataclass
from datetime import date
from pathlib import Path

import asyncpg
import pandas as pd

from . import config as _config
from .gate import CellResult

logger = logging.getLogger(__name__)


@dataclass
class ClassifiedResult:
    """CellResult + classification + overlaps + computed rank."""

    result: CellResult
    status: str  # 'novel' | 'subset' | 'partial_overlap'
    overlaps: dict[str, float]
    overlap_max: float
    rank_score: float
    cell_signature: str
    first_seen_scan_date: date | None  # filled later by persist_run
    n_times_seen: int  # filled later by persist_run


_INSERT_SQL = """
INSERT INTO auto_edge_discoveries (
    scan_run_id, scan_date,
    feature, threshold_op, threshold_value,
    zone, zone_lo, zone_hi, anchor_hours, category, side,
    n_signals, n_unique_markets, win_rate, avg_cost,
    net_roi, mirror_net_roi, mirror_diff_pp,
    year_2025_net_roi, year_2026_net_roi, year_consistency_score,
    daily_signals, daily_raw_pnl_at_5k, daily_honest_pnl_at_5k,
    p_value, bh_adjusted_p, rank_score,
    overlap_pct_d3, overlap_pct_d5, overlap_pct_d7b,
    overlap_pct_d10b, overlap_pct_d13, overlap_pct_d14, overlap_pct_max,
    status, verdict, cell_signature,
    first_seen_scan_date, n_times_seen
) VALUES (
    $1, $2,
    $3, $4, $5,
    $6, $7, $8, $9, $10, $11,
    $12, $13, $14, $15,
    $16, $17, $18,
    $19, $20, $21,
    $22, $23, $24,
    $25, $26, $27,
    $28, $29, $30,
    $31, $32, $33, $34,
    $35, $36, $37,
    $38, $39
)
ON CONFLICT (cell_signature, scan_run_id) DO NOTHING
"""


_SIGNATURE_HISTORY_SQL = """
SELECT first_seen_scan_date, n_times_seen
  FROM auto_edge_discoveries
 WHERE cell_signature = $1
 ORDER BY scan_date DESC
 LIMIT 1
"""


async def get_signature_history(
    conn: asyncpg.Connection,
    signature: str,
) -> tuple[date | None, int]:
    """Return (first_seen_scan_date, n_times_seen+1) for cross-week tracking.

    Returns (None, 1) if signature not seen before.
    """
    row = await conn.fetchrow(_SIGNATURE_HISTORY_SQL, signature)
    if row is None:
        return None, 1
    return row["first_seen_scan_date"], int(row["n_times_seen"]) + 1


async def insert_results(
    conn: asyncpg.Connection,
    scan_run_id: uuid.UUID,
    scan_date: date,
    classified: list[ClassifiedResult],
) -> int:
    """Bulk INSERT classified results into auto_edge_discoveries.

    Skips ON CONFLICT (cell_signature, scan_run_id) — idempotent re-run.
    Returns count of inserted rows.
    """
    n_inserted = 0
    for cr in classified:
        r = cr.result
        c = r.cell
        try:
            await conn.execute(
                _INSERT_SQL,
                scan_run_id,
                scan_date,
                c.feature,
                c.threshold_op,
                float(c.threshold_value),
                c.zone,
                float(c.zone_lo),
                float(c.zone_hi),
                int(c.anchor),
                c.category,
                c.side,
                int(r.n_signals),
                int(r.n_unique_markets),
                float(r.win_rate),
                float(r.avg_cost),
                float(r.net_roi),
                float(r.mirror_net_roi),
                float(r.mirror_diff_pp),
                r.year_2025_net_roi if r.year_2025_net_roi is None else float(r.year_2025_net_roi),
                r.year_2026_net_roi if r.year_2026_net_roi is None else float(r.year_2026_net_roi),
                float(r.year_consistency),
                float(r.daily_signals),
                float(r.daily_raw_pnl),
                float(r.daily_honest_pnl),
                float(r.p_value) if r.p_value is not None else None,
                float(r.bh_adjusted_p) if r.bh_adjusted_p is not None else None,
                float(cr.rank_score),
                float(cr.overlaps.get("D3", 0.0)),
                float(cr.overlaps.get("D5", 0.0)),
                float(cr.overlaps.get("D7b", 0.0)),
                float(cr.overlaps.get("D10b", 0.0)),
                float(cr.overlaps.get("D13", 0.0)),
                float(cr.overlaps.get("D14", 0.0)),
                float(cr.overlap_max),
                cr.status,
                r.verdict,
                cr.cell_signature,
                cr.first_seen_scan_date or scan_date,
                int(cr.n_times_seen),
            )
            n_inserted += 1
        except Exception as exc:
            logger.warning(
                "persist_insert_failed",
                extra={"signature": cr.cell_signature, "error": str(exc)},
            )
    return n_inserted


def render_md_report(
    classified: list[ClassifiedResult],
    scan_date: date,
    cells_total: int,
    cells_evaluated: int,
) -> str:
    """Render markdown report with top-20 candidates."""
    novel = [c for c in classified if c.status == "novel"]
    subset = [c for c in classified if c.status == "subset"]
    partial = [c for c in classified if c.status == "partial_overlap"]

    sorted_all = sorted(classified, key=lambda c: c.rank_score, reverse=True)

    lines = [
        f"# Auto Edge Scan — {scan_date.isoformat()}",
        "",
        "**Source:** `/mnt/data/canonical/ml_features_v2.parquet` (821 days, 63 cols)",
        f"**Cells generated:** {cells_total:,}",
        f"**Cells evaluated:** {cells_evaluated:,}",
        f"**Survived gate:** {len(classified)}",
        f"**Novel:** {len(novel)} | Subset: {len(subset)} | Partial overlap: {len(partial)}",
        "",
        "## Top 20 by rank_score",
        "",
        "| # | feature | op | threshold | zone | anchor | cat | side | n | net_ROI | mirror | year_consist | overlap_max | status | verdict |",
        "|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|",
    ]
    for i, cr in enumerate(sorted_all[:20], start=1):
        r = cr.result
        c = r.cell
        cat = c.category or "ALL"
        lines.append(
            f"| {i} | `{c.feature}` | {c.threshold_op} | {c.threshold_value:.4g} | "
            f"{c.zone} | {c.anchor}h | {cat} | {c.side} | {r.n_signals:,} | "
            f"{r.net_roi*100:+.1f}% | {r.mirror_diff_pp:+.1f}pp | "
            f"{r.year_consistency:.2f} | {cr.overlap_max:.2f} | {cr.status} | {r.verdict} |"
        )

    lines.extend(["", "## Novel candidates (TG-eligible)", ""])
    novel_sorted = sorted(novel, key=lambda c: c.rank_score, reverse=True)
    for i, cr in enumerate(novel_sorted, start=1):
        r = cr.result
        c = r.cell
        cat = c.category or "ALL"
        sustained = (
            f" (seen {cr.n_times_seen}× since {cr.first_seen_scan_date})"
            if cr.n_times_seen >= 2
            else ""
        )
        lines.extend(
            [
                f"### Candidate {i} — `{c.feature} {c.threshold_op} {c.threshold_value:.4g}` × {c.zone} × {c.anchor}h × {cat} × {c.side}{sustained}",
                "",
                f"- **n:** {r.n_signals:,} ({r.n_unique_markets:,} unique markets)",
                f"- **WR:** {r.win_rate*100:.1f}%, avg_cost ${r.avg_cost:.4f}",
                f"- **net ROI:** {r.net_roi*100:+.1f}%",
                f"- **Mirror:** {r.mirror_net_roi*100:+.1f}% (diff {r.mirror_diff_pp:+.1f}pp)",
                f"- **Year split:** 2025 {(r.year_2025_net_roi or 0)*100:+.1f}%, 2026 {(r.year_2026_net_roi or 0)*100:+.1f}% (consistency {r.year_consistency:.2f})",
                f"- **Capacity:** {r.daily_raw_pnl:+.2f}$/d raw, {r.daily_honest_pnl:+.2f}$/d honest @5K",
                f"- **p-value:** {r.p_value:.4g} | **BH q-value:** {r.bh_adjusted_p:.4g}" if r.bh_adjusted_p else f"- **p-value:** {r.p_value:.4g}",
                f"- **Overlap:** max {cr.overlap_max:.2f} ({', '.join(f'{k}={v:.2f}' for k, v in cr.overlaps.items())})",
                f"- **Verdict:** {r.verdict} — **{cr.status.upper()}**",
                "",
            ]
        )

    if not novel_sorted:
        lines.append("_No novel candidates this scan._")
        lines.append("")

    return "\n".join(lines)


def write_md_report(
    classified: list[ClassifiedResult],
    scan_date: date,
    cells_total: int,
    cells_evaluated: int,
    base_dir: str | None = None,
) -> Path:
    """Write MD report file. Returns path."""
    md = render_md_report(classified, scan_date, cells_total, cells_evaluated)
    out_dir = Path(base_dir if base_dir is not None else _config.MD_REPORT_DIR)
    out_dir.mkdir(parents=True, exist_ok=True)
    path = out_dir / f"auto_scan_{scan_date.isoformat()}.md"
    path.write_text(md)
    return path


def write_csv(
    classified: list[ClassifiedResult],
    scan_date: date,
    base_dir: str | None = None,
) -> Path:
    """Write CSV export. Returns path."""
    rows = []
    for cr in classified:
        r = cr.result
        c = r.cell
        rows.append(
            {
                "scan_date": scan_date.isoformat(),
                "feature": c.feature,
                "threshold_op": c.threshold_op,
                "threshold_value": c.threshold_value,
                "zone": c.zone,
                "anchor_hours": c.anchor,
                "category": c.category,
                "side": c.side,
                "n_signals": r.n_signals,
                "n_unique_markets": r.n_unique_markets,
                "win_rate": r.win_rate,
                "avg_cost": r.avg_cost,
                "net_roi": r.net_roi,
                "mirror_net_roi": r.mirror_net_roi,
                "mirror_diff_pp": r.mirror_diff_pp,
                "year_2025_net_roi": r.year_2025_net_roi,
                "year_2026_net_roi": r.year_2026_net_roi,
                "year_consistency": r.year_consistency,
                "daily_signals": r.daily_signals,
                "daily_raw_pnl": r.daily_raw_pnl,
                "daily_honest_pnl": r.daily_honest_pnl,
                "p_value": r.p_value,
                "bh_adjusted_p": r.bh_adjusted_p,
                "rank_score": cr.rank_score,
                "overlap_max": cr.overlap_max,
                "status": cr.status,
                "verdict": r.verdict,
                "cell_signature": cr.cell_signature,
                "n_times_seen": cr.n_times_seen,
            }
        )
    df = pd.DataFrame(rows)
    out_dir = Path(base_dir if base_dir is not None else _config.CSV_OUTPUT_DIR)
    out_dir.mkdir(parents=True, exist_ok=True)
    path = out_dir / f"auto_scan_{int(time.time())}.csv"
    df.to_csv(path, index=False)
    return path


async def get_db_conn() -> asyncpg.Connection:
    """Direct asyncpg connection. Bypasses app config (which has parsing issues)."""
    db_url = os.environ.get("DATABASE_URL")
    if not db_url:
        raise RuntimeError("DATABASE_URL env var not set")
    return await asyncpg.connect(db_url, command_timeout=120)
