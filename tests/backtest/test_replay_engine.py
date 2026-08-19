"""Integration tests для ReplayEngine — stub loader, real D44 detector."""

from __future__ import annotations

from collections.abc import Iterator
from datetime import UTC, datetime
from pathlib import Path

import pytest

from app.backtest.replay_engine import run_backtest
from app.backtest.schema import BacktestConfig, EmitMode, FeeMode, SlippageMode


class StubLoader:
    """In-memory stub loader (no parquet files needed)."""

    def __init__(
        self,
        markets: list[dict],
        snapshots_by_market: dict[str, list[dict]],
        trade_buckets: dict[str, list[dict]] | None = None,
        wallet_score: dict[str, dict[int, dict]] | None = None,
    ) -> None:
        self._markets_list = markets
        self._snapshots_map = snapshots_by_market
        self._buckets_map = trade_buckets or {}
        self._wallet_map = wallet_score or {}

    def filtered_markets(self, config: BacktestConfig):  # noqa: ARG002
        import pandas as pd

        return pd.DataFrame(self._markets_list)

    def iter_market_snapshots(self, markets, tick_stride: int = 1) -> Iterator[tuple[dict, list[dict]]]:
        for mr in markets.to_dict(orient="records"):
            cid = mr["condition_id"]
            snaps = list(self._snapshots_map.get(cid, []))
            snaps.sort(key=lambda s: -int(s["hours_to_resolve"]))
            if tick_stride > 1:
                snaps = snaps[::tick_stride]
            yield mr, snaps

    def trade_buckets_by_market(
        self, condition_ids: set[str] | None = None,
    ) -> dict[str, list[dict]]:
        if condition_ids is None:
            return self._buckets_map
        return {cid: self._buckets_map[cid] for cid in condition_ids if cid in self._buckets_map}

    def wallet_score_by_market(
        self, condition_ids: set[str] | None = None,
    ) -> dict[str, dict[int, dict]]:
        if condition_ids is None:
            return self._wallet_map
        return {cid: self._wallet_map[cid] for cid in condition_ids if cid in self._wallet_map}


def _utc(year: int, month: int, day: int) -> datetime:
    return datetime(year, month, day, tzinfo=UTC)


def _config(**overrides) -> BacktestConfig:
    # Эти тесты проверяют МЕХАНИКУ replay-движка (эмит/win/loss/aggregation),
    # а не политику дефолтов. Пригвождаем legacy-режимы явно, чтобы тесты были
    # детерминированы и не зависели от honest-дефолтов config (см. 2026-06-24).
    base = {
        "detector_name": "d44",
        "start_date": _utc(2020, 1, 1),
        "end_date": _utc(2030, 1, 1),
        "canonical_path": Path("/tmp/nonexistent"),
        "fee_mode": FeeMode.FLAT,
        "slippage_mode": SlippageMode.NONE,
        "emit_mode": EmitMode.FIRST,
    }
    base.update(overrides)
    return BacktestConfig(**base)


def _winning_market_in_d44_band() -> dict:
    """Market that D44 will emit for: crypto, in-band, sufficient volume, yes_won=1.

    Note: D44 category whitelist was changed to Crypto-only (removed Sports).
    """
    return {
        "condition_id": "0xWIN",
        "event_id": "ev_win",
        "question": "Will BTC be above $60K at end of month?",
        "category": "Crypto",
        "lifetime_volume_usd": 1_000_000.0,
        "horizon_days": 10.0,
        "neg_risk": False,
        "yes_won": 1,
        "n_trades_total": 100,
    }


def _losing_market_in_d44_band() -> dict:
    return {
        "condition_id": "0xLOSE",
        "event_id": "ev_lose",
        "question": "Will ETH be above $3K at end of month?",
        "category": "Crypto",
        "lifetime_volume_usd": 1_000_000.0,
        "horizon_days": 10.0,
        "neg_risk": False,
        "yes_won": 0,
        "n_trades_total": 100,
    }


def _snap_at_48h_yes_097(condition_id: str) -> dict:
    return {
        "condition_id": condition_id,
        "hours_to_resolve": 48,
        "yes_price": 0.97,
        "trade_ts": 1700000000,
    }


def test_empty_markets_returns_zero_signals() -> None:
    loader = StubLoader(markets=[], snapshots_by_market={})
    result = run_backtest(_config(), loader=loader)
    assert result.n_signals_emitted == 0
    assert result.n_wins == 0
    assert result.wilson_lower_95 == 0.0


def test_winning_market_emits_winning_signal() -> None:
    market = _winning_market_in_d44_band()
    snap = _snap_at_48h_yes_097(market["condition_id"])
    loader = StubLoader(markets=[market], snapshots_by_market={"0xWIN": [snap]})
    result = run_backtest(_config(), loader=loader)
    assert result.n_signals_emitted == 1
    assert result.n_wins == 1
    assert result.win_rate == 1.0


def test_losing_market_emits_losing_signal() -> None:
    market = _losing_market_in_d44_band()
    snap = _snap_at_48h_yes_097(market["condition_id"])
    loader = StubLoader(markets=[market], snapshots_by_market={"0xLOSE": [snap]})
    result = run_backtest(_config(), loader=loader)
    assert result.n_signals_emitted == 1
    assert result.n_wins == 0
    assert result.win_rate == 0.0


def test_mixed_outcomes_wilson_matches_manual() -> None:
    """5 wins / 10 markets → Wilson lower ≈ 0.237."""
    markets = []
    snaps_map = {}
    for i in range(5):
        m = _winning_market_in_d44_band()
        m["condition_id"] = f"0xWIN{i}"
        markets.append(m)
        snaps_map[m["condition_id"]] = [_snap_at_48h_yes_097(m["condition_id"])]
    for i in range(5):
        m = _losing_market_in_d44_band()
        m["condition_id"] = f"0xLOSE{i}"
        markets.append(m)
        snaps_map[m["condition_id"]] = [_snap_at_48h_yes_097(m["condition_id"])]
    loader = StubLoader(markets=markets, snapshots_by_market=snaps_map)
    result = run_backtest(_config(), loader=loader)
    assert result.n_signals_emitted == 10
    assert result.n_wins == 5
    assert 0.230 < result.wilson_lower_95 < 0.240


def test_out_of_band_market_no_signal() -> None:
    """yes_price=0.5 → outside D44 band → 0 signals."""
    market = _winning_market_in_d44_band()
    snap = _snap_at_48h_yes_097(market["condition_id"])
    snap["yes_price"] = 0.5
    loader = StubLoader(markets=[market], snapshots_by_market={"0xWIN": [snap]})
    result = run_backtest(_config(), loader=loader)
    assert result.n_signals_emitted == 0


def test_first_signal_per_market_emit_first() -> None:
    """EmitMode.FIRST: 3 ticks all in-band → only 1 signal emitted per market."""
    market = _winning_market_in_d44_band()
    snaps = [
        _snap_at_48h_yes_097(market["condition_id"]),
        {**_snap_at_48h_yes_097(market["condition_id"]), "hours_to_resolve": 24},
        {**_snap_at_48h_yes_097(market["condition_id"]), "hours_to_resolve": 12},
    ]
    loader = StubLoader(markets=[market], snapshots_by_market={"0xWIN": snaps})
    result = run_backtest(_config(), loader=loader)
    assert result.n_signals_emitted == 1


def test_per_category_aggregation() -> None:
    """two crypto markets (win + loss) → one category in result."""
    m_win = _winning_market_in_d44_band()
    m_lose = _losing_market_in_d44_band()
    m_lose["condition_id"] = "0xCRYPTO2"
    m_lose["event_id"] = "ev_lose2"
    loader = StubLoader(
        markets=[m_win, m_lose],
        snapshots_by_market={
            "0xWIN": [_snap_at_48h_yes_097("0xWIN")],
            "0xCRYPTO2": [_snap_at_48h_yes_097("0xCRYPTO2")],
        },
    )
    result = run_backtest(_config(), loader=loader)
    assert result.n_signals_emitted == 2
    # Both markets are Crypto — D44 whitelist is Crypto-only since refactor.
    assert set(result.per_category.keys()) == {"Crypto"}
    assert result.per_category["Crypto"].n_wins == 1
    assert result.per_category["Crypto"].n_signals == 2


def test_top_losses_populated_with_highest_confidence_loser() -> None:
    market = _losing_market_in_d44_band()
    snap = _snap_at_48h_yes_097(market["condition_id"])
    loader = StubLoader(markets=[market], snapshots_by_market={"0xLOSE": [snap]})
    result = run_backtest(_config(), loader=loader)
    assert len(result.top_losses) == 1
    assert result.top_losses[0].won is False


def test_deterministic_run() -> None:
    """Two runs same inputs → identical results."""
    market = _winning_market_in_d44_band()
    snap = _snap_at_48h_yes_097(market["condition_id"])
    loader1 = StubLoader(markets=[market], snapshots_by_market={"0xWIN": [snap]})
    loader2 = StubLoader(markets=[market], snapshots_by_market={"0xWIN": [snap]})
    r1 = run_backtest(_config(), loader=loader1)
    r2 = run_backtest(_config(), loader=loader2)
    assert r1.n_signals_emitted == r2.n_signals_emitted
    assert r1.n_wins == r2.n_wins
    assert r1.wilson_lower_95 == r2.wilson_lower_95


def test_config_defaults_are_honest() -> None:
    """Дефолты BacktestConfig honest с 2026-06-24 (post-mortem backtest→prod).

    Регрессия-guard: если кто-то откатит дефолты на FLAT/NONE/FIRST, отчёты
    снова станут вводить в заблуждение. См.
    research/backtest-prod-gap-postmortem-2026-06-24/.
    """
    cfg = BacktestConfig(
        detector_name="d44",
        start_date=_utc(2025, 1, 1),
        end_date=_utc(2026, 1, 1),
    )
    assert cfg.fee_mode is FeeMode.DYNAMIC
    assert cfg.slippage_mode is SlippageMode.REALISTIC
    assert cfg.emit_mode is EmitMode.CRON_INTERVAL


def test_unknown_detector_raises() -> None:
    """Detector not registered → KeyError."""
    loader = StubLoader(markets=[_winning_market_in_d44_band()], snapshots_by_market={"0xWIN": []})
    with pytest.raises(KeyError, match="not registered"):
        run_backtest(_config(detector_name="d_unknown"), loader=loader)


def test_wallet_score_propagates_to_detector_via_run_backtest(monkeypatch):
    """wallet_score доходит до detect() через run_backtest: smart_money_taker_share_4h непустой."""
    from app.backtest import detector_adapter

    captured: list[dict] = []

    def _spy_detect(market_event, now=None):
        captured.append(market_event)
        return None

    monkeypatch.setitem(detector_adapter._REGISTRY, "d34", _spy_detect)

    market = _winning_market_in_d44_band()
    market["category"] = "Sports"
    # Single snapshot at 48h → PIT-safe anchor 72 (anchor 24 кончался бы в end−24h,
    # позже тика в end−48h = look-ahead). Spy replaces d34, его field-checks обойдены.
    snap = _snap_at_48h_yes_097(market["condition_id"])  # hours_to_resolve=48 → PIT-safe anchor 72
    wallet = {market["condition_id"]: {
        72: {"smart_money_taker_share": 0.07, "taker_pnl_weighted_usd": 150.0,
             "taker_roi_weighted": 25.0, "n_takers_24h_before": 5},
    }}
    loader = StubLoader(
        markets=[market],
        snapshots_by_market={market["condition_id"]: [snap]},
        wallet_score=wallet,
    )
    run_backtest(_config(detector_name="d34"), loader=loader)

    assert len(captured) == 1
    assert captured[0]["smart_money_taker_share_4h"] == 0.07


def test_d14_receives_all_wallet_fields_via_run_backtest(monkeypatch):
    """D14 detect получает taker_pnl/roi/n_takers (anchor) + buy_imbalance_asof (buckets) через полный pipeline."""
    from app.backtest import detector_adapter

    captured: list[dict] = []

    def _spy_detect(market_event, now=None):
        captured.append(market_event)
        return None

    monkeypatch.setitem(detector_adapter._REGISTRY, "d14", _spy_detect)

    market = _winning_market_in_d44_band()
    market["condition_id"] = "0xCRYPTO14"
    market["category"] = "Crypto"
    trade_ts = 1_700_000_000
    snap = {
        "condition_id": "0xCRYPTO14",
        "hours_to_resolve": 6,  # PIT-safe anchor 24 (anchor 4 = look-ahead)
        "yes_price": 0.55,
        "trade_ts": trade_ts,
    }
    wallet = {"0xCRYPTO14": {
        24: {"smart_money_taker_share": 0.07, "taker_pnl_weighted_usd": 150.0,
             "taker_roi_weighted": 25.0, "n_takers_24h_before": 5},
    }}
    # buckets в окне [trade_ts-24h, trade_ts]: buy=300, sell=700 → imbalance 0.3 (<0.45)
    buckets = {"0xCRYPTO14": [
        {"bucket_ts_unix": trade_ts - 3600, "total_volume_usd": 1000.0,
         "n_distinct_takers": 5, "vwap_yes": 0.55, "buy_volume": 300.0, "sell_volume": 700.0,
         "n_trades": 10, "min_yes_price": 0.5, "max_yes_price": 0.6},
    ]}
    loader = StubLoader(
        markets=[market],
        snapshots_by_market={"0xCRYPTO14": [snap]},
        trade_buckets=buckets,
        wallet_score=wallet,
    )
    run_backtest(_config(detector_name="d14"), loader=loader, use_trade_buckets=True)

    assert len(captured) == 1
    me = captured[0]
    assert me["taker_pnl_weighted_usd_24h"] == 150.0
    assert me["taker_roi_weighted_24h"] == 25.0
    assert me["n_takers_24h_before"] == 5
    assert abs(me["buy_imbalance_asof"] - 0.3) < 1e-9  # 300/(300+700)
    assert me["smart_money_taker_share_4h"] == 0.07


def test_d14_without_use_trade_buckets_raises() -> None:
    """D14 без use_trade_buckets → ValueError (не молчаливый 0), buy_imbalance REQUIRED."""
    market = _winning_market_in_d44_band()
    market["category"] = "Crypto"
    snap = _snap_at_48h_yes_097(market["condition_id"])
    loader = StubLoader(
        markets=[market],
        snapshots_by_market={market["condition_id"]: [snap]},
    )
    with pytest.raises(ValueError, match="use_trade_buckets"):
        run_backtest(_config(detector_name="d14"), loader=loader)  # use_trade_buckets=False default


def test_use_trade_buckets_integration() -> None:
    """use_trade_buckets=True: buckets индексируются и пробрасываются в adapter, без падений.

    С 2026-05 (PR #498) buckets также подают true 24h volume вместо lifetime-proxy.
    D44 требует volume_24h ≥ $10K и liquidity ≥ $5K. Чтобы сигнал эмитился,
    bucket должен содержать ≥ $200K total (×0.05 liquidity multiplier).
    """
    market = _winning_market_in_d44_band()
    snap = _snap_at_48h_yes_097(market["condition_id"])
    buckets = {
        market["condition_id"]: [
            {
                "bucket_ts_unix": snap["trade_ts"] - 1800,
                "total_volume_usd": 200_000.0,  # PR #498: для D44 liquidity >= $5K
                "n_distinct_takers": 3,
                "vwap_yes": 0.95,
                "buy_volume": 120_000.0,
                "sell_volume": 80_000.0,
                "n_trades": 5,
                "min_yes_price": 0.94,
                "max_yes_price": 0.98,
            },
        ],
    }
    loader = StubLoader(
        markets=[market],
        snapshots_by_market={"0xWIN": [snap]},
        trade_buckets=buckets,
    )
    result = run_backtest(_config(), loader=loader, use_trade_buckets=True)
    # D44 emits — buckets дают volume_24h=$200K и liquidity=$10K (оба ≥ thresholds)
    assert result.n_signals_emitted == 1
    assert result.n_wins == 1


def test_feature_coverage_in_metadata() -> None:
    """metadata.feature_coverage считает None-покрытие derived-фич среди detect-тиков.

    Один snapshot → нет 72h-назад → yes_price_72h_ago=None у всех тиков,
    дошедших до detect(). Диагностика «0 сигналов».
    """
    market = _winning_market_in_d44_band()
    snap = _snap_at_48h_yes_097(market["condition_id"])
    loader = StubLoader(markets=[market], snapshots_by_market={"0xWIN": [snap]})
    result = run_backtest(_config(), loader=loader)

    cov = result.metadata["feature_coverage"]
    reached = result.metadata["n_ticks_reached_detect"]
    assert reached >= 1
    # yes_price_72h_ago None у всех (нет trailing snapshot) → 100%
    assert cov["yes_price_72h_ago"]["none"] == reached
    assert cov["yes_price_72h_ago"]["none_pct"] == 100.0
