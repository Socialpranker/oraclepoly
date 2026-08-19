"""Auto Edge Scanner — end-to-end integration test.

Builds a synthetic 50K-row parquet с injected:
  - One obvious novel edge (mid zone Politics smart_share>0.5 + 24h + NO)
  - One D7b-subset finding (extreme + NO + Politics → high overlap)

Mocks DB writes and TG send. Verifies pipeline correctness.
"""

from __future__ import annotations

from datetime import date
from unittest.mock import patch

import numpy as np
import pandas as pd
import pytest


def _make_synthetic_e2e_parquet(tmp_path):
    """50K rows with controlled distributions для E2E test."""
    rng = np.random.default_rng(42)

    rows = []

    # Block 1: Mid zone, Politics, NO side, smart_share>0.5 → injected NOVEL edge
    # WR=0.55 (NO wins 55% of time → strong asymmetric edge in mid zone)
    n_block1 = 5000
    for i in range(n_block1):
        rows.append({
            "yes_won": int(rng.random() > 0.55),  # NO wins ~55% (strong)
            "yes_price_at_anchor": 0.55,
            "smart_money_taker_share": rng.uniform(0.6, 0.9),  # > 0.5
            "anchor_hours": 24,
            "category": "Politics",
            "condition_id": f"market_a_{i}",
            "resolve_year": 2025 if i % 2 == 0 else 2026,
            # other features filled with defaults
            "top_decile_taker_share": 0.0,
            "taker_pnl_weighted_usd_24h": 0.0,
            "taker_roi_weighted_24h": 0.0,
            "taker_volume_usd_total_24h": 1000.0,
            "n_takers_24h_before": 5,
            "smart_money_taker_volume_usd": 600.0,
            "top_decile_taker_volume_usd": 0.0,
            "whale_buy_pressure_24h": 0.0,
            "whale_taker_volume": 0.0,
            "whale_taker_count": 0,
            "taker_hhi_proxy_24h": 0.5,
            "repeat_taker_ratio_24h": 0.5,
            "retail_taker_avg_size_24h": 100.0,
            "buy_imbalance_asof": 0.5,
            "buy_ratio_24h_before_anchor": 0.5,
            "buy_ratio_72h_before_anchor": 0.5,
            "realized_vol_24h": 0.01,
            "realized_vol_72h": 0.01,
            "max_drawdown_24h_pct": 0.05,
            "vol_24h_concentration": 0.5,
            "vol_24h_pct_asof": 0.5,
            "vpin_proxy_lifetime": 0.5,
            "kyle_lambda_proxy": 0.5,
            "liquidity_quality_proxy": 5.0,
            "drift_72h_to_anchor": 0.0,
            "drift_7d_to_anchor": 0.0,
            "event_arb_distance_yes": 0.0,
            "event_largest_market_share": 0.5,
            "event_thinnest_market_vol": 1000.0,
            "event_yes_sum": 1.0,
            "event_total_lifetime_volume": 10000.0,
            "horizon_days": 1.0,
        })

    # Block 2: Extreme zone, Politics, NO side — D7b territory (subset finding)
    n_block2 = 5000
    for i in range(n_block2):
        rows.append({
            "yes_won": int(rng.random() > 0.85),  # NO wins ~85% (D7b-style)
            "yes_price_at_anchor": 0.90,
            "smart_money_taker_share": 0.03,  # low (matches D13 territory too)
            "anchor_hours": 24,
            "category": "Politics",
            "condition_id": f"market_b_{i}",
            "resolve_year": 2025 if i % 2 == 0 else 2026,
            "top_decile_taker_share": 0.0,
            "taker_pnl_weighted_usd_24h": 0.0,
            "taker_roi_weighted_24h": 0.0,
            "taker_volume_usd_total_24h": 1000.0,
            "n_takers_24h_before": 5,
            "smart_money_taker_volume_usd": 30.0,
            "top_decile_taker_volume_usd": 0.0,
            "whale_buy_pressure_24h": 0.0,
            "whale_taker_volume": 0.0,
            "whale_taker_count": 0,
            "taker_hhi_proxy_24h": 0.5,
            "repeat_taker_ratio_24h": 0.5,
            "retail_taker_avg_size_24h": 100.0,
            "buy_imbalance_asof": 0.5,
            "buy_ratio_24h_before_anchor": 0.5,
            "buy_ratio_72h_before_anchor": 0.5,
            "realized_vol_24h": 0.01,
            "realized_vol_72h": 0.01,
            "max_drawdown_24h_pct": 0.05,
            "vol_24h_concentration": 0.5,
            "vol_24h_pct_asof": 0.5,
            "vpin_proxy_lifetime": 0.5,
            "kyle_lambda_proxy": 0.5,
            "liquidity_quality_proxy": 5.0,
            "drift_72h_to_anchor": 0.0,
            "drift_7d_to_anchor": 0.0,
            "event_arb_distance_yes": 0.0,
            "event_largest_market_share": 0.5,
            "event_thinnest_market_vol": 1000.0,
            "event_yes_sum": 1.0,
            "event_total_lifetime_volume": 10000.0,
            "horizon_days": 1.0,
        })

    df = pd.DataFrame(rows)
    p = tmp_path / "e2e_synthetic.parquet"
    df.to_parquet(p, index=False)
    return str(p)


@pytest.mark.asyncio
async def test_e2e_dry_run_pipeline_completes(tmp_path):
    """Run full pipeline в dry_run mode — no DB, no TG. Verify it doesn't crash."""
    parquet_path = _make_synthetic_e2e_parquet(tmp_path)

    # Patch MD/CSV output dirs to tmp (persist reads cfg attrs at call time)
    import scripts.research.edge_scanner.config as cfg

    md_dir = tmp_path / "docs"
    csv_dir = tmp_path / "outputs"
    md_dir.mkdir()
    csv_dir.mkdir()

    with (
        patch.object(cfg, "MD_REPORT_DIR", str(md_dir)),
        patch.object(cfg, "CSV_OUTPUT_DIR", str(csv_dir)),
    ):
        from scripts.auto_edge_scanner import run_scan

        summary = await run_scan(
            parquet_path=parquet_path,
            dry_run=True,
            no_tg=True,
            scan_date_override=date(2026, 5, 10),
        )

    assert summary["scan_date"] == "2026-05-10"
    assert summary["dry_run"] is True
    assert summary["cells_enumerated"] > 0
    assert summary["tg_alerts_sent"] == 0
    # MD and CSV should be written even in dry_run (only DB skipped)
    md_files = list(md_dir.glob("*.md"))
    csv_files = list(csv_dir.glob("*.csv"))
    assert len(md_files) >= 1
    assert len(csv_files) >= 1


@pytest.mark.asyncio
async def test_e2e_md_report_contains_summary(tmp_path):
    """MD report should have header + cells stats."""
    parquet_path = _make_synthetic_e2e_parquet(tmp_path)

    import scripts.research.edge_scanner.config as cfg

    md_dir = tmp_path / "docs"
    csv_dir = tmp_path / "outputs"
    md_dir.mkdir()
    csv_dir.mkdir()

    with (
        patch.object(cfg, "MD_REPORT_DIR", str(md_dir)),
        patch.object(cfg, "CSV_OUTPUT_DIR", str(csv_dir)),
    ):
        from scripts.auto_edge_scanner import run_scan

        await run_scan(
            parquet_path=parquet_path,
            dry_run=True,
            no_tg=True,
            scan_date_override=date(2026, 5, 10),
        )

    md_files = list(md_dir.glob("*.md"))
    assert len(md_files) == 1
    md_content = md_files[0].read_text()
    assert "Auto Edge Scan" in md_content
    assert "2026-05-10" in md_content
    assert "Top 20 by rank_score" in md_content


@pytest.mark.asyncio
async def test_e2e_csv_export_has_headers(tmp_path):
    parquet_path = _make_synthetic_e2e_parquet(tmp_path)

    import scripts.research.edge_scanner.config as cfg

    md_dir = tmp_path / "docs"
    csv_dir = tmp_path / "outputs"
    md_dir.mkdir()
    csv_dir.mkdir()

    with (
        patch.object(cfg, "MD_REPORT_DIR", str(md_dir)),
        patch.object(cfg, "CSV_OUTPUT_DIR", str(csv_dir)),
    ):
        from scripts.auto_edge_scanner import run_scan

        await run_scan(
            parquet_path=parquet_path,
            dry_run=True,
            no_tg=True,
            scan_date_override=date(2026, 5, 10),
        )

    csv_files = list(csv_dir.glob("*.csv"))
    assert len(csv_files) == 1
    df = pd.read_csv(csv_files[0])
    expected_cols = {
        "scan_date", "feature", "threshold_op", "zone", "anchor_hours",
        "side", "n_signals", "win_rate", "net_roi", "status", "verdict",
        "cell_signature",
    }
    assert expected_cols.issubset(set(df.columns))


def test_cron_runner_registers_auto_edge_scan():
    """auto-edge-scan must be registered in cron_runner."""
    import importlib
    import sys

    # Force reload to pick up edits
    if "scripts.cron_runner" in sys.modules:
        del sys.modules["scripts.cron_runner"]
    runner = importlib.import_module("scripts.cron_runner")
    assert "auto-edge-scan" in runner.JOB_DESCRIPTIONS
    assert "auto-edge-scan" in runner.COMMANDS
    module_path, fn_name = runner.COMMANDS["auto-edge-scan"]
    assert module_path == "app.jobs.auto_edge_scanner"
    assert fn_name == "run_auto_edge_scanner"


@pytest.mark.asyncio
async def test_app_jobs_shim_returns_metadata():
    """The shim should return a dict explaining manual trigger path."""
    from app.jobs.auto_edge_scanner import run_auto_edge_scanner

    result = await run_auto_edge_scanner()
    assert result["status"] == "shim_only"
    assert "venv-research" in result["manual_trigger"]
