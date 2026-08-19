"""Stage 3 / Layer B registration tests."""

from __future__ import annotations

from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent


def test_migration_present():
    assert (REPO_ROOT / "migrations/20260501080002_create_ml_market_picker_table.sql").exists()


def test_market_picker_module_importable():
    from app.calibration import market_picker

    assert hasattr(market_picker, "MarketPickerExample")
    assert hasattr(market_picker, "MarketPickerConfig")
    assert hasattr(market_picker, "train_market_picker")
    assert hasattr(market_picker, "TrainedMarketPicker")
    assert hasattr(market_picker, "top1_per_event_accuracy")


def test_ml_market_picker_fit_job_importable():
    import asyncio

    from app.jobs import ml_market_picker_fit

    assert hasattr(ml_market_picker_fit, "run_ml_market_picker_fit")
    assert asyncio.iscoroutinefunction(ml_market_picker_fit.run_ml_market_picker_fit)


def test_3gate_evaluator_exposed():
    from app.jobs.ml_market_picker_fit import _evaluate_3gate_promotion

    assert callable(_evaluate_3gate_promotion)


def test_cron_runner_registered():
    from scripts.cron_runner import COMMANDS, JOB_DESCRIPTIONS

    assert "ml-market-picker-fit" in COMMANDS
    assert COMMANDS["ml-market-picker-fit"] == (
        "app.jobs.ml_market_picker_fit",
        "run_ml_market_picker_fit",
    )
    assert "ml-market-picker-fit" in JOB_DESCRIPTIONS


def test_crontab_has_layer_b_entry():
    crontab = (REPO_ROOT / "crontab.conf").read_text()
    assert "ml-market-picker-fit" in crontab
    assert "30 4 * * 0" in crontab  # Sun 04:30 UTC


def test_migration_has_3gate_columns():
    sql = (REPO_ROOT / "migrations/20260501080002_create_ml_market_picker_table.sql").read_text()
    assert "hf_holdout_brier" in sql
    assert "live_holdout_brier" in sql
    assert "per_category_metrics" in sql
    assert "top1_per_event_accuracy" in sql
    assert "layer_a_threshold" in sql
