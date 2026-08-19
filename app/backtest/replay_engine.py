"""ReplayEngine — main loop: iterate markets × ticks, emit signals, score outcomes.

Каждый market оценивается на сетке ticks из price_snapshots.parquet. Detector
получает market_event reconstruct'ed по rule build_market_event_at_tick().
Outcome известен из markets_clean.yes_won → SignalOutcome.won computed.

Идемпотентность: один и тот же canonical snapshot+config → identical
SignalOutcome list (sort stable).
"""

from __future__ import annotations

import logging
import time

from app.backtest.data_loader import CanonicalDataLoader
from app.backtest.detector_adapter import (
    build_market_event_at_tick,
    get_detector,
    requires_trade_buckets,
)
from app.backtest.metrics import (
    aggregate_by_category,
    aggregate_roi,
    top_losses_by_confidence,
    wilson_score_interval,
)
from app.backtest.pre_filter import get_pre_filter
from app.backtest.schema import (
    BacktestConfig,
    BacktestResult,
    EmitMode,
    SignalOutcome,
)

logger = logging.getLogger(__name__)

# Derived-фичи, которые детекторы используют как REQUIRED (None → skip). Считаем
# их None-покрытие среди прошедших pre_filter тиков → диагностика «0 сигналов».
# Не влияет на эмиссию, только метаданные. D34: yes_price_72h_ago +
# smart_money_taker_share_4h; D14: + taker_*/n_takers + buy_imbalance_asof.
_FEATURE_PROBES: tuple[str, ...] = (
    "yes_price_72h_ago",
    "smart_money_taker_share_4h",
    "taker_pnl_weighted_usd_24h",
    "taker_roi_weighted_24h",
    "n_takers_24h_before",
    "buy_imbalance_asof",
)


def _signal_won(side: str, yes_won: int) -> bool:
    """side='YES' & yes_won=1 OR side='NO' & yes_won=0."""
    if side == "YES":
        return yes_won == 1
    if side == "NO":
        return yes_won == 0
    return False


def run_backtest(
    config: BacktestConfig,
    loader: CanonicalDataLoader | None = None,
    use_trade_buckets: bool = False,
) -> BacktestResult:
    """Прогнать backtest: load → iterate → score → aggregate.

    Args:
        config: BacktestConfig (включая emit_mode, fee_mode, slippage_mode)
        loader: optional injected loader (для tests)
        use_trade_buckets: загрузить trade_buckets.parquet для feature engineering

    EmitMode семантика:
    - FIRST: один SignalOutcome на market, первый tick где detector эмитит.
      Backwards-compat default. Survivorship bias к winners (markets с band
      exit-re-entry эмитятся только в первый момент входа).
    - PER_TICK: каждый tick где detector эмитит = отдельный SignalOutcome.
      Wilson на correlated ticks overcounts.
    - CRON_INTERVAL: эмулирует prod cron — отдельный SignalOutcome не чаще
      чем раз в config.cron_interval_minutes. Closest to live emission.

    Returns:
        BacktestResult с агрегированными метриками.
    """
    start_ts = time.monotonic()
    if loader is None:
        loader = CanonicalDataLoader(config.canonical_path)

    detect_func = get_detector(config.detector_name)
    if requires_trade_buckets(config.detector_name) and not use_trade_buckets:
        raise ValueError(
            f"Detector '{config.detector_name}' требует use_trade_buckets=True "
            "(REQUIRED-фича приходит только из trade_buckets). Без него detect() "
            "вернёт 0 сигналов молча. Передай use_trade_buckets=True "
            "(CLI: --use-trade-buckets)."
        )
    pre_filter = get_pre_filter(config.detector_name)
    markets = loader.filtered_markets(config)
    if len(markets) == 0:
        logger.warning("backtest_no_markets_filtered", extra={"config": str(config)})
        return _empty_result(config, time.monotonic() - start_ts)

    market_ids = set(markets["condition_id"].astype(str).tolist())
    buckets_by_market: dict[str, list[dict]] | None = None
    if use_trade_buckets:
        buckets_by_market = loader.trade_buckets_by_market(condition_ids=market_ids)
    wallet_by_market = loader.wallet_score_by_market(condition_ids=market_ids)

    outcomes: list[SignalOutcome] = []
    seen_market_ids: set[str] = set()
    seen_event_ids: set[str] = set()
    n_ticks_evaluated = 0
    n_ticks_pre_filtered = 0  # ticks rejected by pre_filter (mirror prod SQL)
    # Диагностика «0 сигналов»: сколько прошедших pre_filter тиков имеют ключевую
    # derived-фичу = None. Detector скипает на None REQUIRED-поле → если фича
    # покрыта плохо, это объясняет пустой результат (см. _FEATURE_PROBES).
    feature_none_counts: dict[str, int] = dict.fromkeys(_FEATURE_PROBES, 0)
    n_ticks_reached_detect = 0

    cron_interval_sec = max(1, config.cron_interval_minutes) * 60

    for market_row, snapshot_dicts in loader.iter_market_snapshots(
        markets,
        tick_stride=config.tick_stride,
    ):
        cid = market_row["condition_id"]
        seen_market_ids.add(cid)
        eid = market_row.get("event_id")
        if eid:
            seen_event_ids.add(str(eid))
        yes_won = int(market_row.get("yes_won", 0))
        emitted_for_market = False
        last_emit_ts: int | None = None

        buckets_for_market = buckets_by_market.get(cid, []) if buckets_by_market is not None else None
        wallet_for_market = wallet_by_market.get(cid)

        for idx, snap in enumerate(snapshot_dicts):
            n_ticks_evaluated += 1
            market_event, tick_now = build_market_event_at_tick(
                market_row,
                snap,
                snapshots_for_market=snapshot_dicts,
                snapshot_idx=idx,
                trade_buckets_for_market=buckets_for_market,
                wallet_score_for_market=wallet_for_market,
            )
            # Mirror prod SQL pre-filter из dN_scan.py. Без этого backtest
            # overcounts density vs prod (D7b: 4376 vs ~1260, см. PR #499).
            if not pre_filter.passes(market_event):
                n_ticks_pre_filtered += 1
                continue
            n_ticks_reached_detect += 1
            for probe in _FEATURE_PROBES:
                if market_event.get(probe) is None:
                    feature_none_counts[probe] += 1
            try:
                signal = detect_func(market_event, now=tick_now)
            except Exception as exc:  # noqa: BLE001
                logger.error(
                    "detector_raise",
                    extra={
                        "detector": config.detector_name,
                        "market_id": cid,
                        "error": str(exc),
                    },
                )
                continue
            if signal is None:
                continue

            # Emit-mode gate: should we record this signal?
            if config.emit_mode is EmitMode.FIRST and emitted_for_market:
                break
            if config.emit_mode is EmitMode.CRON_INTERVAL and last_emit_ts is not None:
                tick_ts = int(snap["trade_ts"])
                if abs(tick_ts - last_emit_ts) < cron_interval_sec:
                    continue

            entry_price = (
                float(market_event["current_price"])
                if signal.side == "YES"
                else 1.0 - float(market_event["current_price"])
            )
            outcomes.append(
                SignalOutcome(
                    market_id=cid,
                    event_id=str(eid) if eid else None,
                    category=market_event.get("category") or "unknown",
                    side=signal.side,
                    entry_price=entry_price,
                    fair_price=float(signal.fair_price),
                    confidence=float(signal.confidence),
                    expected_edge_pct=float(signal.expected_edge_pct),
                    hours_to_resolve=int(snap["hours_to_resolve"]),
                    yes_won=yes_won,
                    won=_signal_won(signal.side, yes_won),
                    question=str(market_row.get("question") or "")[:120],
                )
            )
            emitted_for_market = True
            last_emit_ts = int(snap["trade_ts"])
            if config.emit_mode is EmitMode.FIRST:
                break

    elapsed_sec = time.monotonic() - start_ts
    # Покрытие фич только среди тиков, дошедших до detect() (после pre_filter).
    feature_coverage = {
        probe: {
            "none": none_n,
            "none_pct": round(100.0 * none_n / n_ticks_reached_detect, 1) if n_ticks_reached_detect else 0.0,
        }
        for probe, none_n in feature_none_counts.items()
    }
    return _build_result(
        config,
        outcomes,
        seen_market_ids=seen_market_ids,
        seen_event_ids=seen_event_ids,
        n_ticks_evaluated=n_ticks_evaluated,
        n_ticks_pre_filtered=n_ticks_pre_filtered,
        n_ticks_reached_detect=n_ticks_reached_detect,
        feature_coverage=feature_coverage,
        elapsed_sec=elapsed_sec,
    )


def _empty_result(config: BacktestConfig, elapsed_sec: float) -> BacktestResult:
    return BacktestResult(
        config=config,
        n_signals_emitted=0,
        n_unique_markets=0,
        n_unique_events=0,
        n_wins=0,
        win_rate=0.0,
        wilson_lower_95=0.0,
        wilson_upper_95=0.0,
        avg_edge_pct=0.0,
        per_category={},
        top_losses=[],
        elapsed_sec=elapsed_sec,
        metadata={"reason": "no_markets_matched_filter"},
    )


def _build_result(
    config: BacktestConfig,
    outcomes: list[SignalOutcome],
    *,
    seen_market_ids: set[str],
    seen_event_ids: set[str],
    n_ticks_evaluated: int,
    n_ticks_pre_filtered: int,
    n_ticks_reached_detect: int = 0,
    feature_coverage: dict[str, dict[str, float]] | None = None,
    elapsed_sec: float,
) -> BacktestResult:
    n = len(outcomes)
    wins = sum(1 for o in outcomes if o.won)
    win_rate = wins / n if n > 0 else 0.0
    lower, upper = wilson_score_interval(wins, n, 0.95)
    avg_edge = sum(o.expected_edge_pct for o in outcomes) / n if n > 0 else 0.0
    gross_roi, net_roi = aggregate_roi(
        outcomes,
        fee_mode=config.fee_mode,
        slippage_mode=config.slippage_mode,
        stake_usd=config.stake_usd_per_signal,
    )
    per_cat = aggregate_by_category(
        outcomes,
        0.95,
        fee_mode=config.fee_mode,
        slippage_mode=config.slippage_mode,
        stake_usd=config.stake_usd_per_signal,
    )
    top_losses = top_losses_by_confidence(outcomes, limit=10)
    return BacktestResult(
        config=config,
        n_signals_emitted=n,
        n_unique_markets=len(seen_market_ids),
        n_unique_events=len(seen_event_ids),
        n_wins=wins,
        win_rate=win_rate,
        wilson_lower_95=lower,
        wilson_upper_95=upper,
        avg_edge_pct=avg_edge,
        gross_roi_pct=gross_roi,
        net_roi_pct=net_roi,
        per_category=per_cat,
        top_losses=top_losses,
        elapsed_sec=elapsed_sec,
        metadata={
            "n_ticks_evaluated": n_ticks_evaluated,
            "n_ticks_pre_filtered": n_ticks_pre_filtered,
            "n_ticks_reached_detect": n_ticks_reached_detect,
            "n_markets_with_signal": len({o.market_id for o in outcomes}),
            "feature_coverage": feature_coverage or {},
        },
    )


__all__: list[str] = ["run_backtest"]
