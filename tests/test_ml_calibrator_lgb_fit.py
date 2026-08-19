"""Tests для ml_calibrator_lgb_fit, в первую очередь honest demote semantics.

Контекст 2026-05-19: баг в первой версии — если LGB больше не promotable,
старая active модель оставалась в БД. Это значит fit-job сам считал что
LGB больше не лучше Beta/raw, но inference path продолжал использовать
устаревшую модель. Чинится через unconditional DEACTIVATE_OLD_SQL перед
INSERT'ом нового ряда.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest

pytest.importorskip("lightgbm")

from app.jobs.ml_calibrator_lgb_fit import (  # noqa: E402
    _should_promote,
    run_ml_calibrator_lgb_fit,
)


def _fit_result(detector: str, *, is_active: bool, holdout_brier: float = 0.20) -> dict:
    """Minimal _fit_one_detector result mock."""
    return {
        "version": f"{detector}__v1",
        "detector": detector,
        "model_payload": '{"degenerate": true}',
        "n_train": 800,
        "n_holdout": 200,
        "train_brier": holdout_brier,
        "holdout_brier": holdout_brier,
        "holdout_auc": 0.65,
        "holdout_ece": 0.05,
        "brier_raw_p": 0.25,
        "brier_beta": 0.22,
        "is_active": is_active,
    }


@pytest.mark.asyncio
async def test_demote_deactivates_old_active_even_if_new_not_promoted():
    """Главный тест: если новый fit не promotable, старый is_active=true всё равно
    деактивируется. Это защищает inference от использования stale моделей."""
    # Mock: fetch возвращает training rows для одного детектора
    fake_rows = [
        {
            "detector": "D32_test",
            "confidence": 0.21,
            "expected_edge_pct": 3.0,
            "fair_price": 0.5,
            "current_price": 0.5,
            "horizon_sec": 86400,
            "side": "YES",
            "event_size": 1,
            "outcome": "WIN" if i % 5 == 0 else "LOSS",
            "evidence": {},
            "generated_at": None,
            "category": "Crypto",
            "volume_24h": 1000.0,
            "hours_to_resolution_at_signal": 24.0,
            "recent_wr_30d": 0.2,
            "detector_recent_count_30d": 100,
        }
        for i in range(500)
    ]

    # _fit_one_detector возвращает not-promotable
    mock_fit_result = _fit_result("D32_test", is_active=False)

    execute_calls = []

    async def fake_execute(sql, *args):
        execute_calls.append((sql, args))

    with (
        patch("app.jobs.ml_calibrator_lgb_fit.fetch", new=AsyncMock(return_value=fake_rows)),
        patch("app.jobs.ml_calibrator_lgb_fit.execute", side_effect=fake_execute) as _,
        patch("app.jobs.ml_calibrator_lgb_fit._load_beta_cals", new=AsyncMock(return_value={})),
        patch("app.jobs.ml_calibrator_lgb_fit._fit_one_detector", return_value=mock_fit_result),
    ):
        result = await run_ml_calibrator_lgb_fit()

    # Должно быть 2 execute: 1) DEACTIVATE_OLD_SQL + 2) INSERT нового (is_active=false)
    assert len(execute_calls) == 2

    # 1й вызов — DEACTIVATE (UPDATE ... SET is_active = false)
    deactivate_sql, deactivate_args = execute_calls[0]
    assert "UPDATE" in deactivate_sql.upper()
    assert "is_active = false" in deactivate_sql
    assert deactivate_args == ("D32_test",)

    # 2й вызов — INSERT с is_active=False
    insert_sql, insert_args = execute_calls[1]
    assert "INSERT" in insert_sql.upper()
    # is_active в args — последний параметр
    assert insert_args[-1] is False

    assert result["fitted"] == 1
    assert result["promoted"] == 0


@pytest.mark.asyncio
async def test_promote_also_deactivates_old_active():
    """При promote: тоже сначала deactivate (для unique-index protection),
    потом insert с is_active=true."""
    fake_rows = [
        {
            "detector": "D41_test",
            "confidence": 0.55,
            "expected_edge_pct": 5.0,
            "fair_price": 0.5,
            "current_price": 0.5,
            "horizon_sec": 86400,
            "side": "YES",
            "event_size": 1,
            "outcome": "WIN" if i % 3 == 0 else "LOSS",
            "evidence": {},
            "generated_at": None,
            "category": "Sports",
            "volume_24h": 5000.0,
            "hours_to_resolution_at_signal": 24.0,
            "recent_wr_30d": 0.33,
            "detector_recent_count_30d": 200,
        }
        for i in range(500)
    ]

    mock_fit_result = _fit_result("D41_test", is_active=True, holdout_brier=0.18)
    execute_calls = []

    async def fake_execute(sql, *args):
        execute_calls.append((sql, args))

    with (
        patch("app.jobs.ml_calibrator_lgb_fit.fetch", new=AsyncMock(return_value=fake_rows)),
        patch("app.jobs.ml_calibrator_lgb_fit.execute", side_effect=fake_execute),
        patch("app.jobs.ml_calibrator_lgb_fit._load_beta_cals", new=AsyncMock(return_value={})),
        patch("app.jobs.ml_calibrator_lgb_fit._fit_one_detector", return_value=mock_fit_result),
    ):
        result = await run_ml_calibrator_lgb_fit()

    assert len(execute_calls) == 2
    # 1 deactivate
    assert "UPDATE" in execute_calls[0][0].upper()
    # 2 insert + is_active=True
    assert execute_calls[1][1][-1] is True

    assert result["fitted"] == 1
    assert result["promoted"] == 1


@pytest.mark.asyncio
async def test_skip_does_not_touch_active():
    """Skipped (too few samples) — не трогаем БД, старый active остаётся.
    Это правильно: skip != demote (просто недостаточно данных для fit)."""
    fake_rows = [
        {
            "detector": "D34_tiny",
            "confidence": 0.3,
            "expected_edge_pct": 0.0,
            "fair_price": 0.5,
            "current_price": 0.5,
            "horizon_sec": 86400,
            "side": "YES",
            "event_size": 1,
            "outcome": "WIN",
            "evidence": {},
            "generated_at": None,
            "category": "Crypto",
            "volume_24h": 0.0,
            "hours_to_resolution_at_signal": 24.0,
            "recent_wr_30d": 0.5,
            "detector_recent_count_30d": 5,
        }
        for _ in range(50)  # < MIN_SAMPLES (200)
    ]

    execute_calls = []

    async def fake_execute(sql, *args):
        execute_calls.append((sql, args))

    with (
        patch("app.jobs.ml_calibrator_lgb_fit.fetch", new=AsyncMock(return_value=fake_rows)),
        patch("app.jobs.ml_calibrator_lgb_fit.execute", side_effect=fake_execute),
        patch("app.jobs.ml_calibrator_lgb_fit._load_beta_cals", new=AsyncMock(return_value={})),
    ):
        result = await run_ml_calibrator_lgb_fit()

    # Никаких execute — old active нетронут
    assert execute_calls == []
    assert result["skipped"] == 1
    assert result["fitted"] == 0


# ---------- _should_promote unit tests ----------


def test_should_promote_returns_true_when_lgb_beats_both_by_5_percent():
    assert _should_promote(
        detector="D1", holdout_brier=0.20, brier_raw_p=0.25, brier_beta=0.22, n_holdout=50
    ) is True


def test_should_promote_false_when_lgb_worse_than_raw():
    assert _should_promote(
        detector="D1", holdout_brier=0.26, brier_raw_p=0.25, brier_beta=0.30, n_holdout=50
    ) is False


def test_should_promote_false_when_lgb_worse_than_beta():
    assert _should_promote(
        detector="D1", holdout_brier=0.23, brier_raw_p=0.25, brier_beta=0.22, n_holdout=50
    ) is False


def test_should_promote_false_when_improvement_marginal():
    # 2% improvement < 5% threshold
    assert _should_promote(
        detector="D1", holdout_brier=0.2156, brier_raw_p=0.25, brier_beta=0.22, n_holdout=50
    ) is False


def test_should_promote_false_when_holdout_too_thin():
    assert _should_promote(
        detector="D1", holdout_brier=0.18, brier_raw_p=0.25, brier_beta=0.22, n_holdout=10
    ) is False


def test_should_promote_no_beta_uses_raw_only():
    # Если Beta не существует — сравниваем только с raw_p
    assert _should_promote(
        detector="D1", holdout_brier=0.20, brier_raw_p=0.25, brier_beta=None, n_holdout=50
    ) is True
    # Если LGB не лучше raw — false
    assert _should_promote(
        detector="D1", holdout_brier=0.26, brier_raw_p=0.25, brier_beta=None, n_holdout=50
    ) is False


# ---------- AUC guard (2026-05-19 D14 incident) ----------


def test_should_promote_false_when_auc_below_threshold():
    """D14_crypto LGB случай: Brier лучше raw, но AUC=0.47 (хуже случайного).
    Модель не различает классы → promote запрещён даже если Brier OK."""
    assert _should_promote(
        detector="D14", holdout_brier=0.20, brier_raw_p=0.21,
        brier_beta=None, n_holdout=222, holdout_auc=0.47
    ) is False


def test_should_promote_false_at_auc_boundary():
    """AUC=0.54 (чуть ниже MIN_HOLDOUT_AUC=0.55) → не promote."""
    assert _should_promote(
        detector="D14", holdout_brier=0.20, brier_raw_p=0.25,
        brier_beta=None, n_holdout=222, holdout_auc=0.54
    ) is False


def test_should_promote_true_at_auc_threshold():
    """AUC=0.55 — точно на границе, promote если все остальные условия OK."""
    assert _should_promote(
        detector="D31", holdout_brier=0.20, brier_raw_p=0.25,
        brier_beta=None, n_holdout=222, holdout_auc=0.55
    ) is True


def test_should_promote_auc_none_uses_other_gates():
    """AUC=None означает 1-class holdout — auc guard пропускаем, другие гейты решают."""
    assert _should_promote(
        detector="D31", holdout_brier=0.20, brier_raw_p=0.25,
        brier_beta=None, n_holdout=50, holdout_auc=None
    ) is True


def test_should_promote_default_no_auc_param_backward_compat():
    """Если caller не передал holdout_auc (старый код) — гейт пропускается."""
    assert _should_promote(
        detector="D31", holdout_brier=0.20, brier_raw_p=0.25,
        brier_beta=None, n_holdout=50
    ) is True
