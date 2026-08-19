"""Tests для app.jobs.improvement_cards_monitor."""

from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest

from app.analytics.improvement_cards import TierConfig
from app.jobs.improvement_cards_monitor import (
    _edge_decay_leak_or_none,
    _load_config,
    run_improvement_cards_monitor,
)


@pytest.mark.asyncio
async def test_load_config_returns_tiers_and_thresholds():
    row = {
        "tiers": {"adverse_selection": {"p_fix": 0.6, "hours": 2}},
        "ignored_after_days": 30,
        "stale_after_days": 90,
    }
    with patch("app.jobs.improvement_cards_monitor.fetchrow", new=AsyncMock(return_value=row)):
        tiers, ignored_after, stale_after = await _load_config()
    assert tiers["adverse_selection"] == TierConfig(p_fix_success=0.6, effort_hours=2.0)
    assert ignored_after == 30
    assert stale_after == 90


@pytest.mark.asyncio
async def test_load_config_falls_back_to_defaults():
    with patch("app.jobs.improvement_cards_monitor.fetchrow", new=AsyncMock(return_value=None)):
        tiers, ignored_after, stale_after = await _load_config()
    assert "adverse_selection" in tiers
    assert ignored_after == 30 and stale_after == 90


def test_edge_decay_leak_negative_is_leak():
    # SUM(net_pnl) < 0 → это утечка (возвращаем как есть, отрицательное)
    assert _edge_decay_leak_or_none(-320.0) == pytest.approx(-320.0)


def test_edge_decay_leak_nonnegative_is_none():
    # >= 0 → детектор не течёт деньгами в recent-окне → None (digest-tier)
    assert _edge_decay_leak_or_none(0.0) is None
    assert _edge_decay_leak_or_none(150.0) is None


def test_rowcount_parses_update_result():
    from app.jobs.improvement_cards_monitor import _rowcount

    assert _rowcount("UPDATE 3") == 3
    assert _rowcount("UPDATE 0") == 0
    assert _rowcount("") == 0


@pytest.mark.asyncio
async def test_build_draft_adverse_selection():
    from app.jobs.improvement_cards_monitor import _build_draft, _ClaimRow

    claim = _ClaimRow(
        claim_id=70,
        scope="detector:STANDALONE_V2_LGB",
        statement="adverse selection detected for STANDALONE_V2_LGB×World & Economy",
        evidence_run_ids=[80, 83],
    )
    run = type(
        "R",
        (),
        {
            "run_id": 83,
            "name": "adverse_selection_monitor:STANDALONE_V2_LGB×World & Economy",
            "metrics": {"net_pnl": -15973.95, "is_temperature": True},
        },
    )()
    tiers = {"adverse_selection": TierConfig(0.6, 2.0)}
    with patch("app.jobs.improvement_cards_monitor._fetch_run", new=AsyncMock(return_value=run)):
        draft = await _build_draft(claim, tiers)
    assert draft is not None
    assert draft.card_key == "adverse_selection:detector:STANDALONE_V2_LGB"
    assert draft.leak_usd == pytest.approx(-15973.95)
    assert draft.mechanism_ref.endswith("behavioral_catalog.md#temperature-bots")  # temperature wins
    assert draft.lifecycle_stage == "digest"


@pytest.mark.asyncio
async def test_build_draft_edge_decay_calls_leak_sql():
    from app.jobs.improvement_cards_monitor import _build_draft, _ClaimRow

    claim = _ClaimRow(70, "detector:D38_resolution_lag", "edge decay ...", [8, 11])
    run = type(
        "R",
        (),
        {"run_id": 11, "name": "edge_decay_monitor:detector:D38_resolution_lag", "metrics": {"recent": {"n": 1700}}},
    )()
    tiers = {"edge_decay": TierConfig(0.4, 4.0)}
    with (
        patch("app.jobs.improvement_cards_monitor._fetch_run", new=AsyncMock(return_value=run)),
        patch("app.jobs.improvement_cards_monitor._fetch_edge_decay_leak", new=AsyncMock(return_value=-320.0)),
    ):
        draft = await _build_draft(claim, tiers)
    assert draft.source_kind == "edge_decay"
    assert draft.leak_usd == pytest.approx(-320.0)
    assert draft.lifecycle_stage == "digest"


@pytest.mark.asyncio
async def test_build_draft_skips_non_audit_source():
    from app.jobs.improvement_cards_monitor import _build_draft, _ClaimRow

    claim = _ClaimRow(7, "detector:d42", "sum_roi ...", [1])
    run = type("R", (), {"run_id": 1, "name": "some_other_job:foo", "metrics": {}})()
    with patch("app.jobs.improvement_cards_monitor._fetch_run", new=AsyncMock(return_value=run)):
        draft = await _build_draft(claim, {})
    assert draft is None


def test_upsert_sql_advances_lifecycle_stage_forward_only():
    """ON CONFLICT ОБЯЗАН обновлять lifecycle_stage, и только shadow → digest.

    Регресс-тест прод-инцидента: карточка #2 edge_decay:D38 родилась без leak
    (shadow), leak −$12299 досчитался следующим прогоном, но стадия осталась
    shadow → weekly-бриф (WHERE lifecycle_stage IN ('digest','actionable')) её
    не видел. Утверждение на тексте SQL: _upsert_card в остальных тестах
    замокан целиком, живой SQL там не исполняется.
    """
    from app.jobs.improvement_cards_monitor import _UPSERT_CARD_SQL

    conflict_clause = _UPSERT_CARD_SQL.split("DO UPDATE", 1)[1]
    assert "lifecycle_stage" in conflict_clause, "lifecycle_stage выпал из ON CONFLICT — стадия залипнет"
    # forward-only: digest ставится только поверх shadow, actionable не откатывается.
    assert "improvement_cards.lifecycle_stage = 'shadow'" in conflict_clause
    assert "ELSE improvement_cards.lifecycle_stage" in conflict_clause


@pytest.mark.asyncio
async def test_run_upserts_and_persists_summary():
    from app.jobs.improvement_cards_monitor import _ClaimRow

    claims = [_ClaimRow(70, "detector:STANDALONE_V2_LGB", "adverse ...", [83])]
    draft_sentinel = type(
        "D",
        (),
        {
            "card_key": "adverse_selection:detector:STANDALONE_V2_LGB",
            "claim_id": 70,
            "leak_usd": -15973.95,
            "p_fix_success": 0.6,
            "effort_hours": 2.0,
        },
    )()
    with (
        patch(
            "app.jobs.improvement_cards_monitor._load_config",
            new=AsyncMock(return_value=({"adverse_selection": TierConfig(0.6, 2.0)}, 30, 90)),
        ),
        patch("app.jobs.improvement_cards_monitor._fetch_candidate_claims", new=AsyncMock(return_value=claims)),
        patch("app.jobs.improvement_cards_monitor._build_draft", new=AsyncMock(return_value=draft_sentinel)),
        patch("app.jobs.improvement_cards_monitor._upsert_card", new=AsyncMock()) as upsert,
        patch("app.jobs.improvement_cards_monitor._reconcile_lifecycle", new=AsyncMock(return_value=(2, 1))),
        patch("app.jobs.improvement_cards_monitor._persist_run", new=AsyncMock()) as persist,
    ):
        result = await run_improvement_cards_monitor()
    assert result["n_candidates"] == 1
    assert result["n_cards_upserted"] == 1
    assert result["n_ignored"] == 2 and result["n_stale"] == 1
    assert result["top_ev"] == pytest.approx(4792.185)  # |−15973.95| × 0.6 / 2
    assert result["n_key_collisions"] == 0
    upsert.assert_awaited_once_with(draft_sentinel)
    persist.assert_awaited_once()


@pytest.mark.asyncio
async def test_run_logs_collision_on_duplicate_card_key():
    from app.jobs.improvement_cards_monitor import _ClaimRow

    # Два claim'а (напр. adverse-монитор per detector×category без категории в
    # scope) резолвятся в один card_key — коллизия должна залогироваться и
    # НЕ должна прервать обработку (upsert вызывается для обоих, побеждает
    # последний = старший id благодаря ORDER BY id в _CANDIDATE_CLAIMS_SQL).
    claims = [
        _ClaimRow(70, "detector:D41_temperature", "adverse selection W&E ...", [80]),
        _ClaimRow(71, "detector:D41_temperature", "adverse selection Sports ...", [81]),
    ]
    draft1 = type(
        "D",
        (),
        {
            "card_key": "adverse_selection:detector:D41_temperature",
            "claim_id": 70,
            "leak_usd": -100.0,
            "p_fix_success": 0.6,
            "effort_hours": 2.0,
        },
    )()
    draft2 = type(
        "D",
        (),
        {
            "card_key": "adverse_selection:detector:D41_temperature",
            "claim_id": 71,
            "leak_usd": -200.0,
            "p_fix_success": 0.6,
            "effort_hours": 2.0,
        },
    )()
    with (
        patch(
            "app.jobs.improvement_cards_monitor._load_config",
            new=AsyncMock(return_value=({"adverse_selection": TierConfig(0.6, 2.0)}, 30, 90)),
        ),
        patch("app.jobs.improvement_cards_monitor._fetch_candidate_claims", new=AsyncMock(return_value=claims)),
        patch("app.jobs.improvement_cards_monitor._build_draft", new=AsyncMock(side_effect=[draft1, draft2])),
        patch("app.jobs.improvement_cards_monitor._upsert_card", new=AsyncMock()) as upsert,
        patch("app.jobs.improvement_cards_monitor._reconcile_lifecycle", new=AsyncMock(return_value=(0, 0))),
        patch("app.jobs.improvement_cards_monitor._persist_run", new=AsyncMock()),
    ):
        result = await run_improvement_cards_monitor()
    assert result["n_key_collisions"] == 1
    assert upsert.await_count == 2


@pytest.mark.asyncio
async def test_run_does_not_send_telegram():
    # Компонент 4 НЕ шлёт TG. Если случайно появится импорт send_telegram_message —
    # этот тест страхует границу: модуль не должен его импортировать.
    import app.jobs.improvement_cards_monitor as mod

    assert not hasattr(mod, "send_telegram_message")
