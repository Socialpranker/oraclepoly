"""CLI runner для backtest framework.

Usage:
    # Default = honest mode с 2026-06-24 (dynamic fee, realistic slippage,
    # cron_interval emit). Net ROI — primary метрика вердикта, Wilson — вторична:
    python scripts/run_backtest.py --detector d44 \
        --start 2025-06-01 --end 2026-04-30 \
        --use-trade-buckets \
        --out backtest_report.md

    # Legacy режим (flat 3% fee, no slippage, first-emit) — только для
    # воспроизведения старых отчётов, НЕ для решений (вводит в заблуждение):
    python scripts/run_backtest.py --detector d44 \
        --start 2025-06-01 --end 2026-04-30 \
        --fee-mode flat --slippage-mode none --emit-mode first \
        --out backtest_report.md

См. research/backtest-prod-gap-postmortem-2026-06-24/ — почему дефолты honest.
"""

from __future__ import annotations

import argparse
import logging
import sys
from datetime import UTC, datetime
from pathlib import Path

# Add project root to path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.backtest import BacktestConfig  # noqa: E402
from app.backtest.replay_engine import run_backtest  # noqa: E402
from app.backtest.report import format_markdown_report  # noqa: E402
from app.backtest.schema import EmitMode, FeeMode, SlippageMode  # noqa: E402

DEFAULT_CANONICAL = Path("/mnt/data/canonical")


def _parse_date(s: str) -> datetime:
    """YYYY-MM-DD → tz-aware UTC datetime."""
    return datetime.strptime(s, "%Y-%m-%d").replace(tzinfo=UTC)


def main() -> int:
    parser = argparse.ArgumentParser(description="Run paired tick replay backtest")
    parser.add_argument("--detector", required=True, help="Detector name (d44, d45, ...)")
    parser.add_argument("--start", required=True, type=_parse_date, help="Start date YYYY-MM-DD")
    parser.add_argument("--end", required=True, type=_parse_date, help="End date YYYY-MM-DD (exclusive)")
    parser.add_argument("--canonical", default=str(DEFAULT_CANONICAL), help="Canonical layer path")
    parser.add_argument("--category", action="append", default=None,
                        help="Filter by category (repeatable). E.g. --category sports --category crypto")
    parser.add_argument("--max-markets", type=int, default=None, help="Cap N markets для quick test")
    parser.add_argument("--tick-stride", type=int, default=1, help="Every Nth snapshot (default 1 = all)")
    parser.add_argument("--out", default="backtest_report.md", help="Output markdown path")
    parser.add_argument("--quiet", action="store_true", help="Disable INFO logs")
    parser.add_argument(
        "--use-trade-buckets",
        action="store_true",
        help=(
            "Load trade_buckets (~501 MB). Включает (a) feature engineering "
            "для D34/D41/D_esports, (b) ОБЯЗАТЕЛЕН для D14 (buy_imbalance_asof — "
            "REQUIRED, иначе 0 сигналов с hard-error), (c) ВНИМАНИЕ — true 24h "
            "volume/liquidity вместо lifetime-proxy (раньше завышал density для "
            "underdog detectors). Recommended: ON для honest backtest."
        ),
    )
    parser.add_argument(
        "--fee-mode",
        choices=[m.value for m in FeeMode],
        default=FeeMode.DYNAMIC.value,
        help="Fee model: dynamic (default, per-signal fee_calculator) or flat (legacy 3%%)",
    )
    parser.add_argument(
        "--slippage-mode",
        choices=[m.value for m in SlippageMode],
        default=SlippageMode.REALISTIC.value,
        help="Slippage haircut: realistic (default, per-category), baseline (2%%), none (legacy)",
    )
    parser.add_argument(
        "--emit-mode",
        choices=[m.value for m in EmitMode],
        default=EmitMode.CRON_INTERVAL.value,
        help="Emission: cron_interval (default, prod-like), first (one per market, legacy), per_tick",
    )
    parser.add_argument(
        "--stake-usd",
        type=float,
        default=100.0,
        help="Stake $ per signal (для расчёта dynamic fees)",
    )
    parser.add_argument(
        "--cron-interval-minutes",
        type=int,
        default=5,
        help="Min interval между сигналами в EmitMode.CRON_INTERVAL",
    )
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.WARNING if args.quiet else logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )

    category_filter = frozenset(args.category) if args.category else None
    config = BacktestConfig(
        detector_name=args.detector,
        start_date=args.start,
        end_date=args.end,
        canonical_path=Path(args.canonical),
        category_filter=category_filter,
        max_markets=args.max_markets,
        tick_stride=args.tick_stride,
        fee_mode=FeeMode(args.fee_mode),
        slippage_mode=SlippageMode(args.slippage_mode),
        emit_mode=EmitMode(args.emit_mode),
        stake_usd_per_signal=args.stake_usd,
        cron_interval_minutes=args.cron_interval_minutes,
    )

    print(f"Running backtest: detector={args.detector}, "
          f"range={args.start.date()}→{args.end.date()}, "
          f"max_markets={args.max_markets}, tick_stride={args.tick_stride}",
          file=sys.stderr)

    result = run_backtest(config, use_trade_buckets=args.use_trade_buckets)
    report = format_markdown_report(result)
    out_path = Path(args.out)
    # Создаём родительский каталог — иначе write_text падает FileNotFoundError
    # на несуществующем пути ПОСЛЕ полного прогона (минуты впустую). Симметрично
    # bulk_backtest.py, который mkdir(parents=True) делает заранее.
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(report, encoding="utf-8")

    # Brief summary to stderr (always)
    print(
        f"DONE: n_signals={result.n_signals_emitted}, "
        f"win_rate={result.win_rate:.4f}, "
        f"wilson_lower_95={result.wilson_lower_95:.4f}, "
        f"elapsed={result.elapsed_sec:.2f}s → {out_path}",
        file=sys.stderr,
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
