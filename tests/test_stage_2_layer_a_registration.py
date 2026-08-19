"""Stage 2 / Layer A registration tests.

Проверяют что все компоненты Layer A связаны: миграция, job module, cron registration, crontab.
"""

from __future__ import annotations

from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent


def test_migration_present():
    assert (REPO_ROOT / "migrations/20260501080001_create_ml_event_selector_table.sql").exists()


def test_event_selector_module_importable():
    from app.calibration import event_selector

    assert hasattr(event_selector, "EventSelectorExample")
    assert hasattr(event_selector, "EventSelectorConfig")
    assert hasattr(event_selector, "train_event_selector")
    assert hasattr(event_selector, "TrainedEventSelector")


def test_ml_event_selector_fit_job_importable():
    import asyncio

    from app.jobs import ml_event_selector_fit

    assert hasattr(ml_event_selector_fit, "run_ml_event_selector_fit")
    assert asyncio.iscoroutinefunction(ml_event_selector_fit.run_ml_event_selector_fit)


def test_cron_runner_registered():
    from scripts.cron_runner import COMMANDS, JOB_DESCRIPTIONS

    assert "ml-event-selector-fit" in COMMANDS
    assert COMMANDS["ml-event-selector-fit"] == (
        "app.jobs.ml_event_selector_fit",
        "run_ml_event_selector_fit",
    )
    assert "ml-event-selector-fit" in JOB_DESCRIPTIONS


def test_crontab_has_layer_a_entry():
    crontab = (REPO_ROOT / "crontab.conf").read_text()
    assert "ml-event-selector-fit" in crontab
    # Sun 03:30 UTC
    assert "30 3 * * 0" in crontab


def test_archetype_check_constraint_in_migration():
    sql = (REPO_ROOT / "migrations/20260501080001_create_ml_event_selector_table.sql").read_text()
    # Layer A table — нет CHECK constraint на archetype поскольку это просто хранилище модели
    # Но имеет UNIQUE constraint на is_active
    assert "uq_ml_event_selector_active" in sql
    assert "WHERE is_active = true" in sql
