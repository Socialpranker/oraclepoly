"""Markdown report formatter для BacktestResult.

Один файл — один backtest run. Используется в scripts/run_backtest.py.
"""

from __future__ import annotations

from datetime import UTC, datetime

from app.backtest.schema import BacktestResult


def format_markdown_report(result: BacktestResult) -> str:
    """Format BacktestResult → human-readable markdown."""
    cfg = result.config
    lines: list[str] = []
    lines.append(f"# Backtest report — {cfg.detector_name.upper()}")
    lines.append("")
    lines.append(f"> Generated: {datetime.now(UTC).isoformat()}")
    lines.append("")
    lines.append("## Configuration")
    lines.append("")
    lines.append(f"- **Detector:** `{cfg.detector_name}`")
    lines.append(f"- **Date range:** {cfg.start_date.date()} → {cfg.end_date.date()}")
    lines.append(f"- **Canonical path:** `{cfg.canonical_path}`")
    if cfg.category_filter:
        lines.append(f"- **Category filter:** {sorted(cfg.category_filter)}")
    if cfg.max_markets:
        lines.append(f"- **Max markets cap:** {cfg.max_markets}")
    if cfg.tick_stride > 1:
        lines.append(f"- **Tick stride:** every {cfg.tick_stride}th snapshot")
    lines.append(f"- **Emit mode:** {cfg.emit_mode.value}")
    lines.append(f"- **Fee mode:** {cfg.fee_mode.value}")
    lines.append(f"- **Slippage mode:** {cfg.slippage_mode.value}")
    lines.append(f"- **Stake (for fee/slippage):** ${cfg.stake_usd_per_signal:.0f}")
    lines.append(f"- **Elapsed:** {result.elapsed_sec:.2f}s")
    lines.append("")
    lines.append("## Headline metrics")
    lines.append("")
    lines.append(f"- **Signals emitted:** {result.n_signals_emitted}")
    lines.append(f"- **Unique markets:** {result.n_unique_markets}")
    lines.append(f"- **Unique events:** {result.n_unique_events}")
    lines.append(f"- **Wins:** {result.n_wins}")
    lines.append(f"- **Win rate:** {result.win_rate:.4f}")
    lines.append(f"- **Wilson lower 95%:** **{result.wilson_lower_95:.4f}**")
    lines.append(f"- **Wilson upper 95%:** {result.wilson_upper_95:.4f}")
    lines.append(f"- **Avg expected edge %:** {result.avg_edge_pct:.2f}%")
    lines.append(f"- **Gross ROI %/signal:** {result.gross_roi_pct:+.2f}%")
    net_label = _format_net_label(cfg)
    lines.append(f"- **Net ROI %/signal** ({net_label}): **{result.net_roi_pct:+.2f}%**")
    lines.append("")
    if result.metadata:
        lines.append("## Metadata")
        lines.append("")
        for k, v in sorted(result.metadata.items()):
            lines.append(f"- `{k}`: {v}")
        lines.append("")
    if result.per_category:
        lines.append("## Per-category breakdown")
        lines.append("")
        lines.append("| Category | N | Wins | Win rate | Wilson lower | Gross ROI | Net ROI |")
        lines.append("|---|---|---|---|---|---|---|")
        for cat in sorted(result.per_category, key=lambda c: -result.per_category[c].n_signals):
            s = result.per_category[cat]
            lines.append(
                f"| {s.category} | {s.n_signals} | {s.n_wins} | {s.win_rate:.4f} | "
                f"{s.wilson_lower_95:.4f} | {s.gross_roi_pct:+.2f}% | {s.net_roi_pct:+.2f}% |"
            )
        lines.append("")
    if result.top_losses:
        lines.append("## Top-10 losing signals (sorted by confidence)")
        lines.append("")
        lines.append("| Confidence | Side | Entry | Fair | DTE h | Category | Question |")
        lines.append("|---|---|---|---|---|---|---|")
        for o in result.top_losses:
            q = o.question.replace("|", "\\|")
            lines.append(
                f"| {o.confidence:.3f} | {o.side} | {o.entry_price:.4f} | {o.fair_price:.4f} | "
                f"{o.hours_to_resolve} | {o.category} | {q} |"
            )
        lines.append("")
    lines.append("## Verdict (Net ROI primary, Wilson secondary)")
    lines.append("")
    net_roi = result.net_roi_pct
    n = result.n_signals_emitted
    if n == 0:
        verdict = "❓ **N=0** — no signals emitted, cannot evaluate."
    elif n < 30:
        verdict = f"⏳ **HOLD (low-N)** — only {n} signals. Bound unreliable below N=30."
    elif net_roi >= 5.0:
        verdict = f"✅ **KEEPER** — Net ROI **{net_roi:+.2f}%/signal** ≥ 5%. Wilson lower {result.wilson_lower_95:.4f}."
    elif net_roi >= 0.0:
        verdict = f"⚠ **HOLD** — Net ROI {net_roi:+.2f}%/signal in [0%, 5%). Wilson lower {result.wilson_lower_95:.4f}."
    else:
        verdict = (
            f"❌ **KILL** — Net ROI **{net_roi:+.2f}%/signal** negative after fees. "
            f"Wilson lower {result.wilson_lower_95:.4f}."
        )
    lines.append(verdict)
    lines.append("")
    lines.append(
        f"> Net ROI = gross - fee - slippage. Fee mode `{cfg.fee_mode.value}`, "
        f"slippage mode `{cfg.slippage_mode.value}`. "
        "Caveat: survivorship bias — canonical contains only resolved markets, "
        "UMA disputes underrepresented (≈2pp win-rate inflation). "
        "Liquidity proxied lifetime_volume / horizon_days."
    )
    lines.append("")
    return "\n".join(lines)


def _format_net_label(cfg) -> str:
    """Human label для Net ROI row reflecting current modes."""
    from app.backtest.schema import FeeMode, SlippageMode

    fee_lbl = "3% flat" if cfg.fee_mode is FeeMode.FLAT else "dynamic fees"
    if cfg.slippage_mode is SlippageMode.NONE:
        slip_lbl = "no slippage"
    elif cfg.slippage_mode is SlippageMode.BASELINE:
        slip_lbl = "2% slippage"
    else:
        slip_lbl = "per-cat slippage"
    return f"{fee_lbl} + {slip_lbl}"
