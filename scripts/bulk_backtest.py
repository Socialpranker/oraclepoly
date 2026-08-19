"""Bulk-backtest всех зарегистрированных детекторов на canonical historical.

Запускает run_backtest для каждого detector в registry. Output:
1. research/paired-tick-replay-2026-05/d<N>_<date>.md per detector
2. research/paired-tick-replay-2026-05/_bulk_summary.md — summary table

Usage:
    python scripts/bulk_backtest.py \
        --start 2024-01-01 --end 2026-05-01 \
        --out-dir research/paired-tick-replay-2026-05
"""

from __future__ import annotations

import argparse
import logging
import sys
import time
from datetime import UTC, datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.backtest import BacktestConfig  # noqa: E402
from app.backtest.data_loader import CanonicalDataLoader  # noqa: E402
from app.backtest.detector_adapter import _REGISTRY, requires_trade_buckets  # noqa: E402
from app.backtest.replay_engine import run_backtest  # noqa: E402
from app.backtest.report import format_markdown_report  # noqa: E402
from app.backtest.schema import BacktestResult  # noqa: E402


def _parse_date(s: str) -> datetime:
    return datetime.strptime(s, "%Y-%m-%d").replace(tzinfo=UTC)


# Per-detector overrides — детекторы со специфичными требованиями к выборке.
# Без них bulk на широком all-category окне ловит OOM: d14 на полном датасете
# берёт 98% markets → 15.4M trade_bucket dict → ~13 ГБ RSS. d14 = "crypto winners
# distribute NO", его домен ТОЛЬКО crypto → фильтр режет выборку до 14% buckets
# (~3.6 ГБ). Эталонный d14_diag.md всегда гнался с --category crypto.
#   category: список категорий для category_filter (None = все)
_DETECTOR_OVERRIDES: dict[str, dict[str, object]] = {
    "d14": {"category": ["crypto"]},
}


def _verdict_label(result: BacktestResult) -> str:
    """Net-ROI based verdict (matches report.py logic).

    Fade-strategy detectors часто торгуют low-prob high-payoff: win rate
    низкий, но payoff high. Wilson lower на win rate misleads — primary
    metric net ROI.
    """
    n = result.n_signals_emitted
    if n == 0:
        return "N=0"
    if n < 30:
        return "low-N"
    net_roi = result.net_roi_pct
    if net_roi >= 5.0:
        return "KEEPER"
    if net_roi >= 0.0:
        return "HOLD"
    return "KILL"


def _summary_md(results: list[tuple[str, BacktestResult]]) -> str:
    lines = [
        f"# Bulk backtest summary — {datetime.now(UTC).isoformat()}",
        "",
        "| Detector | N | Win rate | Wilson lower | Gross ROI | **Net ROI** | Verdict |",
        "|---|---|---|---|---|---|---|",
    ]
    # Sort: KEEPER > HOLD > low-N > KILL > N=0
    rank = {"KEEPER": 0, "HOLD": 1, "low-N": 2, "KILL": 3, "N=0": 4}
    results_sorted = sorted(results, key=lambda x: (rank[_verdict_label(x[1])], -x[1].net_roi_pct))
    for name, r in results_sorted:
        verdict = _verdict_label(r)
        lines.append(
            f"| `{name}` | {r.n_signals_emitted} | {r.win_rate:.4f} | "
            f"{r.wilson_lower_95:.4f} | {r.gross_roi_pct:+.2f}% | "
            f"**{r.net_roi_pct:+.2f}%** | **{verdict}** |"
        )
    lines.append("")
    lines.append("## Verdict categories (Net ROI primary)")
    lines.append("")
    lines.append("- **KEEPER**: Net ROI ≥ 5%/signal (strong positive EV after fees)")
    lines.append("- **HOLD**: Net ROI in [0%, 5%) — marginal positive")
    lines.append("- **KILL**: Net ROI < 0% — no edge survives fees")
    lines.append("- **low-N**: < 30 signals — bound unreliable")
    lines.append("- **N=0**: no signals emitted — detector filter mismatch")
    lines.append("")
    lines.append("## Why Net ROI, not Win Rate?")
    lines.append("")
    lines.append(
        "Многие detectors (D5/D7b/D31/etc) — fade strategies со ставками на "
        "low-probability high-payoff outcomes. Win rate 15% но payoff 10x → "
        "expected value strongly positive. Wilson lower на win rate misleads — "
        "это правильно для прогнозов \"будет ли вероятность ≥ 50%\", но не "
        "для торговых стратегий с asymmetric payoff."
    )
    lines.append("")
    lines.append("## Caveats")
    lines.append("")
    lines.append(
        "- **Survivorship bias**: canonical = resolved markets only. "
        "Skip rate (cancelled markets) не учтён."
    )
    lines.append(
        "- **Liquidity proxy**: volume_24h = lifetime_volume / horizon_days. "
        "На thin markets adapter может пропустить filter что в live не сработал бы."
    )
    lines.append(
        "- **No slippage**: backtest assume fills at quoted yes_price. Real "
        "execution имеет market impact, особенно на extreme tails (D32, D5)."
    )
    lines.append(
        "- **3% fee approximation**: round-trip uniform. Реально zone-aware "
        "(см. app/services/fee_calculator.py) — мог занижать у Sports, "
        "завышать у extreme."
    )
    lines.append("")
    return "\n".join(lines)


def main() -> int:
    parser = argparse.ArgumentParser(description="Bulk-backtest всех registered детекторов")
    parser.add_argument("--start", required=True, type=_parse_date)
    parser.add_argument("--end", required=True, type=_parse_date)
    parser.add_argument("--canonical", default="/mnt/data/canonical")
    parser.add_argument("--out-dir", default="research/paired-tick-replay-2026-05")
    parser.add_argument("--tick-stride", type=int, default=4)
    parser.add_argument("--max-markets", type=int, default=None)
    parser.add_argument("--skip", action="append", default=[], help="Detector name to skip (repeatable)")
    parser.add_argument(
        "--use-trade-buckets",
        action="store_true",
        help=(
            "Разрешить загрузку trade_buckets (~501 MB) для детекторов, которым "
            "она REQUIRED (см. _BUCKETS_REQUIRED — сейчас только d14). Грузится "
            "per-detector, НЕ для всех: иначе детектор без category-фильтра тянет "
            "все 15.4M строк → OOM."
        ),
    )
    args = parser.parse_args()

    logging.basicConfig(level=logging.WARNING, format="%(asctime)s %(levelname)s %(name)s: %(message)s")

    out_dir = Path(args.out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)

    # Single shared loader — кэшируем parquet один раз для всех детекторов
    loader = CanonicalDataLoader(Path(args.canonical))
    print("Pre-loading canonical layer (markets + snapshots)...", file=sys.stderr)
    loader.load_markets()
    loader.load_snapshots()

    detectors = sorted(_REGISTRY.keys())
    print(f"Registered detectors: {detectors}", file=sys.stderr)
    detectors = [d for d in detectors if d not in args.skip]
    print(f"Will run: {detectors}", file=sys.stderr)

    today = datetime.now(UTC).strftime("%Y%m%d")
    results: list[tuple[str, BacktestResult]] = []
    for name in detectors:
        t0 = time.monotonic()
        override = _DETECTOR_OVERRIDES.get(name, {})
        categories = override.get("category")
        category_filter = frozenset(categories) if categories else None
        if category_filter:
            print(f"  [{name}] override: --category {sorted(category_filter)}", file=sys.stderr)
        cfg = BacktestConfig(
            detector_name=name,
            start_date=args.start,
            end_date=args.end,
            canonical_path=Path(args.canonical),
            category_filter=category_filter,
            tick_stride=args.tick_stride,
            max_markets=args.max_markets,
        )
        # Сбросить bucket/wallet-кэш: разные детекторы имеют разные category_filter
        # → разные condition_ids → нельзя переиспользовать кэш предыдущего.
        loader.reset_feature_caches()
        # buckets грузим ТОЛЬКО детекторам, которым они REQUIRED (d14). Глобально
        # на всех = OOM: детектор без category-фильтра тянет все 15.4M bucket-строк.
        use_buckets = args.use_trade_buckets and requires_trade_buckets(name)
        try:
            result = run_backtest(cfg, loader=loader, use_trade_buckets=use_buckets)
            results.append((name, result))
            elapsed = time.monotonic() - t0
            print(
                f"  {name}: n={result.n_signals_emitted} "
                f"win={result.win_rate:.4f} wilson_lower={result.wilson_lower_95:.4f} "
                f"({elapsed:.1f}s)",
                file=sys.stderr,
            )
            report_path = out_dir / f"{name}_{today}.md"
            report_path.write_text(format_markdown_report(result), encoding="utf-8")
        except Exception as exc:  # noqa: BLE001
            print(f"  {name}: ERROR — {exc}", file=sys.stderr)
            continue

    summary_path = out_dir / f"_bulk_summary_{today}.md"
    summary_path.write_text(_summary_md(results), encoding="utf-8")
    print(f"\nSummary written to {summary_path}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
