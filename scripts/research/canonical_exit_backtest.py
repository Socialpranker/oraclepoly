"""Адаптер: canonical-replay (app/backtest/) -> exit-симуляторы (exit_strategy_backtest.py).

Контекст: research/position-exit-strategies-2026-07/ — прод-бэктест на реальных
virtual-сигналах (exit_strategy_backtest.py) покрыл только 5 недель / 2 детектора,
т.к. detector_signal_price_snapshots начинаются с 2026-05-28 и прод-сигналы вообще
не пересекаются во времени с canonical (тот заморожен на 2026-05-04). Единственный
путь получить широкое историческое покрытие exit-симуляций — пересчитать детектор
ЗАНОВО через app/backtest/ поверх price_snapshots.parquet (2024-01 — 2026-05).

Не патчит app/backtest/replay_engine.py напрямую (используется для promote-гейтов,
менять рискованно). Это отдельная, минимальная копия его цикла — только то, что
нужно для получения ценовой траектории ПОСЛЕ тика эмита сигнала (run_backtest()
такое не отдаёт наружу, только агрегированный BacktestResult).

Ограничение по объёму (verified 2026-07-03, см. research/position-exit-strategies-2026-07/):
только d44 (легковесный, НЕ требует trade_buckets — исключает документированный
риск OOM на широких детекторах типа d14, research/backtest-prod-gap-postmortem-2026-06-24).
price_snapshots.parquet — грубая 8-точечная сетка (4h/8h/12h/24h/48h/72h/168h/720h
to-resolve), не непрерывная траектория — trailing stop/time-decay увидят только эти
дискретные точки, разрешение хуже прод-снапшотов (там медиана 144 точки на позицию).

Запускать ТОЛЬКО на проде (canonical недоступен локально):
    docker run --rm -v /root/oracle:/app -v /mnt/data/canonical:/mnt/data/canonical:ro \
        -w /app oracle-backend:latest python -m scripts.research.canonical_exit_backtest \
        --detector d44 --start 2024-01-01 --end 2026-05-01
"""

from __future__ import annotations

import argparse
from dataclasses import dataclass
from datetime import UTC, datetime

import pandas as pd

from app.backtest.data_loader import CanonicalDataLoader
from app.backtest.detector_adapter import build_market_event_at_tick, get_detector
from app.backtest.pre_filter import get_pre_filter
from app.backtest.schema import DEFAULT_CANONICAL_PATH
from scripts.research.exit_strategy_backtest import (
    PositionTrajectory,
    PricePoint,
    _sortino_ratio,
    simulate_partial_exit,
    simulate_time_decay,
    simulate_trailing_stop,
)


@dataclass
class CanonicalSignalTrajectory:
    market_id: str
    detector: str
    side: str
    category: str
    entry_price: float
    filled_at: pd.Timestamp
    expected_resolution_at: pd.Timestamp
    yes_won: int
    snapshots_after_entry: list[PricePoint]


def _signal_won(side: str, yes_won: int) -> bool:
    if side == "YES":
        return yes_won == 1
    if side == "NO":
        return yes_won == 0
    return False


def build_trajectories_for_detector(
    *,
    detect_func,
    loader,
    config_start: datetime,
    config_end: datetime,
    category_filter: frozenset[str] | None,
    tick_stride: int = 1,
) -> list[CanonicalSignalTrajectory]:
    """Минимальная копия цикла app/backtest/replay_engine.run_backtest(), но
    вместо агрегации в BacktestResult — сохраняет per-signal траекторию ПОСЛЕ
    тика эмита (нужно для exit-симуляторов, run_backtest() это не отдаёт).

    EmitMode эквивалент FIRST (один сигнал на market, первый эмитящий тик) —
    достаточно для exit-симуляции: нас интересует "что было бы после входа",
    не плотность эмиссии (та важна для Wilson score, не для нашей задачи).
    """

    class _Config:
        pass

    cfg = _Config()
    cfg.start_date = config_start
    cfg.end_date = config_end
    cfg.category_filter = category_filter
    cfg.max_markets = None

    markets = loader.filtered_markets(cfg)
    if len(markets) == 0:
        return []

    trajectories: list[CanonicalSignalTrajectory] = []

    for market_row, snapshot_dicts in loader.iter_market_snapshots(markets, tick_stride=tick_stride):
        cid = market_row["condition_id"]
        yes_won = int(market_row.get("yes_won", 0))
        category = market_row.get("category") or "unknown"
        end_date = market_row["end_date"]

        for idx, snap in enumerate(snapshot_dicts):
            market_event, tick_now = build_market_event_at_tick(
                market_row,
                snap,
                snapshots_for_market=snapshot_dicts,
                snapshot_idx=idx,
                trade_buckets_for_market=None,
                wallet_score_for_market=None,
            )
            try:
                signal = detect_func(market_event, now=tick_now)
            except Exception:  # noqa: BLE001
                continue
            if signal is None:
                continue

            entry_price = (
                float(market_event["current_price"])
                if signal.side == "YES"
                else 1.0 - float(market_event["current_price"])
            )
            hours_to_resolve_at_entry = int(snap["hours_to_resolve"])
            filled_at = pd.Timestamp(end_date) - pd.Timedelta(hours=hours_to_resolve_at_entry)

            snapshots_after_entry = [
                PricePoint(captured_at=pd.Timestamp(end_date) - pd.Timedelta(hours=int(s["hours_to_resolve"])), yes_price=float(s["yes_price"]))
                for s in snapshot_dicts[idx + 1 :]
            ]

            trajectories.append(
                CanonicalSignalTrajectory(
                    market_id=cid,
                    detector=signal.side,  # placeholder, перезаписывается вызывающим кодом
                    side=signal.side,
                    category=category,
                    entry_price=entry_price,
                    filled_at=filled_at,
                    expected_resolution_at=pd.Timestamp(end_date),
                    yes_won=yes_won,
                    snapshots_after_entry=snapshots_after_entry,
                )
            )
            break  # FIRST-emit эквивалент: один сигнал на market

    return trajectories


def to_position_trajectory(ct: CanonicalSignalTrajectory, *, stake: float) -> PositionTrajectory:
    """Конвертирует CanonicalSignalTrajectory в PositionTrajectory — формат,
    который уже принимают simulate_trailing_stop/simulate_time_decay/simulate_partial_exit.

    category lowercased — app.services.fee_calculator._resolve_category ожидает
    lowercase (canonical хранит Title-case: 'Crypto', 'Sports').
    fee_schedule всегда None — canonical НЕ содержит per-market fee_schedule
    (verified 2026-07-03), fee_calculator падает в hardcoded category fallback.
    """
    return PositionTrajectory(
        order_id=f"{ct.market_id}:{ct.filled_at.isoformat()}",
        detector=ct.detector,
        side=ct.side,
        stake=stake,
        fill_price=ct.entry_price,
        category=(ct.category or "other").lower(),
        fee_schedule=None,
        filled_at=ct.filled_at,
        snapshots=ct.snapshots_after_entry,
        expected_resolution_at=ct.expected_resolution_at,
    )


def run_canonical_exit_backtest(
    *,
    detector_name: str,
    start: datetime,
    end: datetime,
    canonical_path=DEFAULT_CANONICAL_PATH,
    stake: float = 100.0,
) -> dict:
    loader = CanonicalDataLoader(canonical_path)
    detect_func = get_detector(detector_name)
    pre_filter = get_pre_filter(detector_name)

    raw_trajectories = build_trajectories_for_detector(
        detect_func=detect_func,
        loader=loader,
        config_start=start,
        config_end=end,
        category_filter=None,
    )
    # pre_filter применяется внутри build_market_event_at_tick-цикла у прод-джобы,
    # здесь используем тот же pre_filter, чтобы mirror-ить prod density — но т.к.
    # detect_func сам по себе может вернуть None без pre_filter, для d44 (лёгкий
    # детектор без REQUIRED derived-фич из pre_filter) это не критично; оставлено
    # явно для будущих детекторов, где pre_filter обязателен.
    _ = pre_filter  # зарезервировано, см. докстринг выше

    for ct in raw_trajectories:
        ct.detector = detector_name

    baseline_pnls = []
    trailing_pnls = []
    decay_pnls = []
    partial_pnls = []

    from app.jobs.v2_execution_resolve import _compute_settlement_pnl

    for ct in raw_trajectories:
        pt = to_position_trajectory(ct, stake=stake)

        outcome = "WIN" if _signal_won(ct.side, ct.yes_won) else "LOSS"
        baseline = _compute_settlement_pnl(
            stake=stake, fill_price=ct.entry_price, outcome=outcome, category=pt.category, fee_schedule=None
        )
        baseline_pnls.append(baseline["net_pnl"])

        trail_result = simulate_trailing_stop(pt, trail_pct=15.0)
        if trail_result:
            trailing_pnls.append(trail_result.net_pnl)

        decay_result = simulate_time_decay(
            pt, wide_sl_pct=-30.0, narrow_sl_pct=-10.0, wide_tp_pct=40.0, narrow_tp_pct=15.0
        )
        if decay_result:
            decay_pnls.append(decay_result.net_pnl)

        partial_result = simulate_partial_exit(
            pt, partial_trigger_pct=15.0, partial_fraction=0.5, actual_outcome=outcome
        )
        if partial_result:
            partial_pnls.append(partial_result.net_pnl)

    return {
        "detector": detector_name,
        "n_signals": len(raw_trajectories),
        "baseline": {
            "n": len(baseline_pnls),
            "total_net_pnl": round(sum(baseline_pnls), 2),
            "sortino": _sortino_ratio(baseline_pnls),
        },
        "trailing_stop_15pct": {
            "n": len(trailing_pnls),
            "total_net_pnl": round(sum(trailing_pnls), 2),
            "sortino": _sortino_ratio(trailing_pnls),
        },
        "time_decay": {
            "n": len(decay_pnls),
            "total_net_pnl": round(sum(decay_pnls), 2),
            "sortino": _sortino_ratio(decay_pnls),
        },
        "partial_exit_50pct": {
            "n": len(partial_pnls),
            "total_net_pnl": round(sum(partial_pnls), 2),
            "sortino": _sortino_ratio(partial_pnls),
        },
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--detector", required=True, help="lowercase, e.g. d44")
    parser.add_argument("--start", required=True, help="YYYY-MM-DD")
    parser.add_argument("--end", required=True, help="YYYY-MM-DD")
    parser.add_argument("--canonical", default=str(DEFAULT_CANONICAL_PATH))
    args = parser.parse_args()

    from pathlib import Path

    start = datetime.strptime(args.start, "%Y-%m-%d").replace(tzinfo=UTC)
    end = datetime.strptime(args.end, "%Y-%m-%d").replace(tzinfo=UTC)

    result = run_canonical_exit_backtest(
        detector_name=args.detector, start=start, end=end, canonical_path=Path(args.canonical)
    )

    import json

    print(json.dumps(result, indent=2, default=str))


if __name__ == "__main__":
    main()
