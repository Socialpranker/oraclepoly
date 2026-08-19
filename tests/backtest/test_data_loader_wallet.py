"""Tests для wallet_score loader в CanonicalDataLoader."""

from __future__ import annotations

from pathlib import Path

import pandas as pd


def _write_wallet_parquet(tmp_path: Path) -> Path:
    df = pd.DataFrame(
        [
            {"condition_id": "0xaaa", "anchor_hours": 4, "n_takers_24h_before": 5,
             "taker_volume_usd_total": 1000.0, "smart_money_taker_volume_usd": 300.0,
             "top_decile_taker_volume_usd": 200.0, "taker_roi_weighted": 25.0,
             "taker_pnl_weighted_usd": 150.0, "smart_money_taker_share": 0.30,
             "top_decile_taker_share": 0.20},
            {"condition_id": "0xaaa", "anchor_hours": 24, "n_takers_24h_before": 8,
             "taker_volume_usd_total": 2000.0, "smart_money_taker_volume_usd": 100.0,
             "top_decile_taker_volume_usd": 50.0, "taker_roi_weighted": 12.0,
             "taker_pnl_weighted_usd": 80.0, "smart_money_taker_share": 0.05,
             "top_decile_taker_share": 0.025},
            {"condition_id": "0xbbb", "anchor_hours": 4, "n_takers_24h_before": 3,
             "taker_volume_usd_total": 500.0, "smart_money_taker_volume_usd": 50.0,
             "top_decile_taker_volume_usd": 25.0, "taker_roi_weighted": 8.0,
             "taker_pnl_weighted_usd": 20.0, "smart_money_taker_share": 0.10,
             "top_decile_taker_share": 0.05},
        ]
    )
    path = tmp_path / "wallet_score_at_anchor.parquet"
    df.to_parquet(path)
    return tmp_path


def test_wallet_score_by_market_indexes_by_condition_and_anchor(tmp_path):
    from app.backtest.data_loader import CanonicalDataLoader

    canonical = _write_wallet_parquet(tmp_path)
    loader = CanonicalDataLoader(canonical)
    wmap = loader.wallet_score_by_market()

    assert set(wmap.keys()) == {"0xaaa", "0xbbb"}
    assert set(wmap["0xaaa"].keys()) == {4, 24}
    assert wmap["0xaaa"][4]["smart_money_taker_share"] == 0.30
    assert wmap["0xaaa"][24]["smart_money_taker_share"] == 0.05
    assert wmap["0xbbb"][4]["taker_pnl_weighted_usd"] == 20.0


def test_wallet_score_by_market_filters_condition_ids(tmp_path):
    from app.backtest.data_loader import CanonicalDataLoader

    canonical = _write_wallet_parquet(tmp_path)
    loader = CanonicalDataLoader(canonical)
    wmap = loader.wallet_score_by_market(condition_ids={"0xaaa"})
    assert set(wmap.keys()) == {"0xaaa"}


def test_wallet_score_missing_file_returns_empty(tmp_path):
    """Нет parquet → пустой dict (backtest деградирует, detect() сам skip)."""
    from app.backtest.data_loader import CanonicalDataLoader

    loader = CanonicalDataLoader(tmp_path)  # пустая директория
    assert loader.wallet_score_by_market() == {}
