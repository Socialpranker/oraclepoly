"""Тесты detector_signal_service: L1 + L2 inference path."""

from __future__ import annotations

import asyncio
import json
import random
from datetime import UTC, datetime
from unittest.mock import AsyncMock, patch
from uuid import uuid4

import pytest

from app.calibration.beta_calibrator import BetaCalibrator
from app.calibration.ev_sizer import (
    SizingDecision,
    size_from_calibrated_p,
    size_from_raw_confidence,
)
from app.calibration.meta_labeler import (
    MetaLabelerConfig,
    MetaLabelerExample,
    serialize_to_json,
    train_meta_labeler,
)
from app.services.detector_signal_service import (
    _DROPPED_SIGNALS_TELEMETRY_SQL,
    _FETCH_FRESH_SIGNALS_SQL,
    _FETCH_SIGNAL_BY_ID_SQL,
    _SIGNAL_SELECT_BASE_SQL,
    _calibrate,
    _emit_cal_anchor_shadow,
    _log_lifecycle_dropped_signals,
    fetch_calibrated_detector_signals,
    fetch_calibrated_signal_by_id,
)


def _row(**overrides):
    base = {
        "id": uuid4(),
        "detector": "D1_fade_whale",
        "market_id": "mkt-1",
        "event_id": "evt-1",
        "side": "NO",
        "raw_p": 0.7,
        "expected_edge_pct": 80.4,
        "fair_price": 0.42,
        "entry_price": 0.45,
        "horizon_sec": 1800,
        "token_id": "tok-1",
        "evidence": {"tx_hash": "0xabc"},
        "generated_at": datetime.now(UTC),
        "category": "Sports",
        "volume_24h": 50000.0,
    }
    base.update(overrides)
    return base


def _fitted_calibrator():
    rng = random.Random(0)
    raw_p = [rng.uniform(0.3, 0.8) for _ in range(50)]
    outcome = [1 if rng.random() < 0.6 else 0 for _ in range(50)]
    cal = BetaCalibrator(min_samples=30)
    cal.fit(raw_p, outcome)
    return cal


def test_fetch_sql_uses_side_aware_entry_price():
    """SQL должен возвращать entry_price = current_price (YES) | 1-current_price (NO).

    До фикса использовалось `ds.current_price AS entry_price` без side-conversion,
    что для NO-сигналов давало entry_price ≈ цена YES (баг для EV-sizer).
    """
    sql = _FETCH_FRESH_SIGNALS_SQL
    # Проверяем CASE WHEN side = 'YES' ... ELSE 1.0 - current_price
    assert "ds.side = 'YES'" in sql
    assert "1.0 - ds.current_price" in sql
    assert "AS entry_price" in sql
    # Не должно быть голого `ds.current_price    AS entry_price`
    assert "ds.current_price    AS entry_price" not in sql


def test_calibrate_returns_raw_when_calibrator_missing():
    # _calibrate теперь async (G18 bin lookup tier 0 требует await).
    # Без feature flag G18 skip, fall-through на raw.
    p, src = asyncio.run(_calibrate(0.7, "D1_fade_whale", {}))
    assert p == 0.7
    assert src == "raw"


def test_calibrate_applies_calibrator_when_present():
    cal = _fitted_calibrator()
    cals = {"D1_fade_whale": cal}
    p, src = asyncio.run(_calibrate(0.7, "D1_fade_whale", cals))
    assert 0 < p < 1
    assert src == "beta"


@pytest.mark.asyncio
async def test_fetch_no_signals_returns_empty():
    with patch(
        "app.services.detector_signal_service.fetch",
        new_callable=AsyncMock,
        return_value=[],
    ):
        result = await fetch_calibrated_detector_signals()
    assert result == []


@pytest.mark.asyncio
async def test_fetch_passes_lock_timeout_for_cron_list():
    """Cron-список проставляет lock_timeout_ms (защита от VACUUM FULL markets)."""
    fetch_mock = AsyncMock(return_value=[])
    with patch("app.services.detector_signal_service.fetch", fetch_mock):
        await fetch_calibrated_detector_signals()
    # Первый вызов — сам fetch сигналов (после него after_fetch делает свой fetch).
    first_call = fetch_mock.call_args_list[0]
    assert first_call.kwargs.get("lock_timeout_ms") == 3000


@pytest.mark.asyncio
async def test_fetch_lock_timeout_fired_returns_empty_no_raise():
    """QueryCanceledError (lock_timeout сработал) → пустой тик, job НЕ падает."""
    import asyncpg

    fetch_mock = AsyncMock(side_effect=asyncpg.exceptions.QueryCanceledError("canceled"))
    with patch("app.services.detector_signal_service.fetch", fetch_mock):
        result = await fetch_calibrated_detector_signals()
    assert result == []


@pytest.mark.asyncio
async def test_fetch_cold_start_no_calibrators_no_l2():
    """Без L1 и L2 — meta_p == raw_p (cold-start)."""
    rows = [_row(raw_p=0.7)]

    fetch_mock = AsyncMock()
    # 2 fetch calls через сервис: signals + lifecycle-dropped telemetry (PR-D)
    fetch_mock.side_effect = [rows, []]
    # Загрузчики калибраторов fetch'ат через fallback_chain: L1 + LGB
    chain_fetch_mock = AsyncMock()
    chain_fetch_mock.side_effect = [[], []]

    with (
        patch("app.services.detector_signal_service.fetch", fetch_mock),
        patch("app.calibration.fallback_chain.fetch", chain_fetch_mock),
        patch(
            "app.services.detector_signal_service.fetchrow",
            new_callable=AsyncMock,
            return_value=None,
        ),
    ):
        result = await fetch_calibrated_detector_signals()

    assert len(result) == 1
    sig = result[0]
    assert sig["raw_p"] == 0.7
    assert sig["calibrated_p"] == 0.7  # L1 missing → fallback
    assert sig["meta_p"] == 0.7  # L2 missing → meta_p == calibrated_p
    assert sig["detector"] == "D1_fade_whale"


@pytest.mark.asyncio
async def test_fetch_with_l1_only_applies_calibrator():
    """L1 fit'ed, L2 нет — meta_p == calibrated_p (после Beta), не raw."""
    rows = [_row(raw_p=0.7)]
    cal = _fitted_calibrator()
    cals_rows = [{"detector": "D1_fade_whale", "params": json.dumps(cal.serialize())}]

    fetch_mock = AsyncMock()
    # 2 fetch calls через сервис: signals + lifecycle-dropped telemetry (PR-D)
    fetch_mock.side_effect = [rows, []]
    # Загрузчики калибраторов fetch'ат через fallback_chain: L1 + LGB
    chain_fetch_mock = AsyncMock()
    chain_fetch_mock.side_effect = [cals_rows, []]

    with (
        patch("app.services.detector_signal_service.fetch", fetch_mock),
        patch("app.calibration.fallback_chain.fetch", chain_fetch_mock),
        patch(
            "app.services.detector_signal_service.fetchrow",
            new_callable=AsyncMock,
            return_value=None,
        ),
    ):
        result = await fetch_calibrated_detector_signals()

    assert len(result) == 1
    sig = result[0]
    assert sig["calibrated_p"] != sig["raw_p"]  # калибратор применился
    assert sig["meta_p"] == sig["calibrated_p"]  # L2 нет → meta_p == calibrated_p


@pytest.mark.asyncio
async def test_fetch_full_pipeline_l1_and_l2():
    """L1 + L2 fit'ed — meta_p из LightGBM, не calibrated_p."""
    rows = [_row(raw_p=0.7) for _ in range(3)]

    cal = _fitted_calibrator()
    cals_rows = [{"detector": "D1_fade_whale", "params": json.dumps(cal.serialize())}]

    # Train L2 на synthetic dataset
    examples = []
    rng = random.Random(0)
    for _ in range(200):
        examples.append(
            MetaLabelerExample(
                detector_id="D1_fade_whale",
                calibrated_p=rng.uniform(0.3, 0.8),
                edge_pct=80.0,
                entry_price=0.4,
                horizon_sec=1800,
                category="Sports",
                volume_24h=50000.0,
                is_v2_post=True,
                outcome=1 if rng.random() < 0.6 else 0,
            )
        )
    model = train_meta_labeler(examples, config=MetaLabelerConfig(min_train_samples=100))
    model_payload = json.loads(serialize_to_json(model))

    fetch_mock = AsyncMock()
    # 2 fetch calls через сервис: signals + lifecycle-dropped telemetry (PR-D)
    fetch_mock.side_effect = [rows, []]
    # Загрузчики калибраторов fetch'ат через fallback_chain: L1 + LGB
    chain_fetch_mock = AsyncMock()
    chain_fetch_mock.side_effect = [cals_rows, []]

    fetchrow_mock = AsyncMock(return_value={"model_payload": model_payload})

    with (
        patch("app.services.detector_signal_service.fetch", fetch_mock),
        patch("app.calibration.fallback_chain.fetch", chain_fetch_mock),
        patch("app.services.detector_signal_service.fetchrow", fetchrow_mock),
    ):
        result = await fetch_calibrated_detector_signals()

    assert len(result) == 3
    for sig in result:
        assert 0.0 <= sig["meta_p"] <= 1.0
        # meta_p может быть равен calibrated_p теоретически, но не raw
        assert sig["calibrated_p"] != sig["raw_p"]


@pytest.mark.asyncio
async def test_end_to_end_signal_to_stake_full_pipeline():
    """Full inference: detector_signals → L1 → L2 → EV-sizer → stake/skip.

    Проверяет integration между всеми компонентами Phase E:
    1. fetch_calibrated_detector_signals() возвращает signal с meta_p
    2. size_from_calibrated_p(meta_p, entry_price, bankroll) возвращает решение
    3. high-edge сигналы → stake_usd > 0
    """
    rows = [_row(raw_p=0.7, entry_price=0.4)]
    cal = _fitted_calibrator()
    cals_rows = [{"detector": "D1_fade_whale", "params": json.dumps(cal.serialize())}]

    fetch_mock = AsyncMock()
    # 2 fetch calls через сервис: signals + lifecycle-dropped telemetry (PR-D)
    fetch_mock.side_effect = [rows, []]
    # Загрузчики калибраторов fetch'ат через fallback_chain: L1 + LGB
    chain_fetch_mock = AsyncMock()
    chain_fetch_mock.side_effect = [cals_rows, []]

    with (
        patch("app.services.detector_signal_service.fetch", fetch_mock),
        patch("app.calibration.fallback_chain.fetch", chain_fetch_mock),
        patch(
            "app.services.detector_signal_service.fetchrow",
            new_callable=AsyncMock,
            return_value=None,  # без L2, cold-start через L1
        ),
    ):
        signals = await fetch_calibrated_detector_signals()

    assert len(signals) == 1
    sig = signals[0]

    decision = size_from_calibrated_p(
        calibrated_p=sig["meta_p"],
        entry_price=sig["entry_price"],
        bankroll_usd=1000.0,
    )

    assert isinstance(decision, SizingDecision)
    assert decision.stake_usd is not None
    assert decision.reason == "ok"
    assert decision.stake_usd > 0
    assert decision.stake_usd <= 50.0  # 5% bankroll cap


@pytest.mark.asyncio
async def test_end_to_end_cold_start_uses_raw_confidence_path():
    """Cold-start (нет L1+L2) → используем size_from_raw_confidence с f=0.10."""
    rows = [_row(raw_p=0.7, entry_price=0.4)]

    fetch_mock = AsyncMock()
    # 2 fetch calls через сервис: signals + lifecycle-dropped telemetry (PR-D)
    fetch_mock.side_effect = [rows, []]
    # Загрузчики калибраторов fetch'ат через fallback_chain: L1 + LGB (нет calibrators)
    chain_fetch_mock = AsyncMock()
    chain_fetch_mock.side_effect = [[], []]

    with (
        patch("app.services.detector_signal_service.fetch", fetch_mock),
        patch("app.calibration.fallback_chain.fetch", chain_fetch_mock),
        patch(
            "app.services.detector_signal_service.fetchrow",
            new_callable=AsyncMock,
            return_value=None,
        ),
    ):
        signals = await fetch_calibrated_detector_signals()

    sig = signals[0]
    # cold-start: meta_p == raw_p
    assert sig["meta_p"] == sig["raw_p"]

    cold_decision = size_from_raw_confidence(
        raw_confidence=sig["raw_p"],
        entry_price=sig["entry_price"],
        bankroll_usd=1000.0,
    )
    full_decision = size_from_calibrated_p(
        calibrated_p=sig["raw_p"],  # один и тот же p
        entry_price=sig["entry_price"],
        bankroll_usd=1000.0,
    )

    # Кохолд-старт даёт меньшую ставку (f=0.10 vs f=0.25), либо равную если оба упёрлись в cap
    assert cold_decision.stake_usd is not None
    assert full_decision.stake_usd is not None
    assert cold_decision.stake_usd <= full_decision.stake_usd


# ─── Fix-3 (latency-аудит №3): sizing-телеметрия молчала с рождения ──────────
# SQL выбирает ds.current_price / yes_token_id / ds.fair_price, но enriched-dict
# их терял → sig.get("current_price") в v2_execution всегда None → точка 'sizing'
# в price_staleness_log = 0 строк за всё время (prod-верификация 2026-07-07).


@pytest.mark.asyncio
async def test_fetch_propagates_price_telemetry_fields():
    """current_price/yes_token_id/fair_price из SQL доходят до sig-словаря."""
    rows = [_row(current_price=0.55, yes_token_id="tok-yes", fair_price=0.38)]

    fetch_mock = AsyncMock()
    fetch_mock.side_effect = [rows, []]
    chain_fetch_mock = AsyncMock()
    chain_fetch_mock.side_effect = [[], []]

    with (
        patch("app.services.detector_signal_service.fetch", fetch_mock),
        patch("app.calibration.fallback_chain.fetch", chain_fetch_mock),
        patch(
            "app.services.detector_signal_service.fetchrow",
            new_callable=AsyncMock,
            return_value=None,
        ),
    ):
        result = await fetch_calibrated_detector_signals()

    sig = result[0]
    assert sig["current_price"] == 0.55
    assert sig["yes_token_id"] == "tok-yes"
    assert sig["fair_price"] == 0.38


@pytest.mark.asyncio
async def test_fetch_tolerates_missing_price_telemetry_fields():
    """Строки без новых полей (старые моки/юнит-тесты) → None, не KeyError."""
    rows = [_row()]  # _row не содержит current_price/yes_token_id
    del rows[0]["fair_price"]

    fetch_mock = AsyncMock()
    fetch_mock.side_effect = [rows, []]
    chain_fetch_mock = AsyncMock()
    chain_fetch_mock.side_effect = [[], []]

    with (
        patch("app.services.detector_signal_service.fetch", fetch_mock),
        patch("app.calibration.fallback_chain.fetch", chain_fetch_mock),
        patch(
            "app.services.detector_signal_service.fetchrow",
            new_callable=AsyncMock,
            return_value=None,
        ),
    ):
        result = await fetch_calibrated_detector_signals()

    sig = result[0]
    assert sig["current_price"] is None
    assert sig["yes_token_id"] is None
    assert sig["fair_price"] is None


# ─── Денежный гейт: stage IN ('virtual','live') ──────────────────────────────
#
# Единственное место, где paused/shadow детекторы отсекаются от денег.
# Эмиссию сигналов stage НЕ гасит (это делает cron) — см. docs/detector_lifecycle.md
# §"Два независимых рубильника". Если этот JOIN ослабнет до LEFT или в список
# stage'ей попадёт 'paused'/'shadow', детектор на паузе пойдёт в v2_execution
# и начнёт тратить деньги молча. Тесты ниже стерегут ровно это.


_GATED_SQLS = [
    ("base", _SIGNAL_SELECT_BASE_SQL),
    ("fresh", _FETCH_FRESH_SIGNALS_SQL),
    ("by_id", _FETCH_SIGNAL_BY_ID_SQL),
]
# ids по имени, иначе pytest печатает весь SQL в имени теста (50KB вывода).
_GATED_IDS = [name for name, _ in _GATED_SQLS]


@pytest.mark.parametrize("sql_name, sql", _GATED_SQLS, ids=_GATED_IDS)
def test_money_gate_joins_detector_state_inner(sql_name, sql):
    """INNER JOIN на detector_state — не LEFT: незарегистрированный детектор к деньгам не идёт."""
    assert "JOIN detector_state dst" in sql, f"{sql_name}: пропал JOIN на detector_state"
    # LEFT JOIN пропустил бы детектор без строки в detector_state (dst.stage IS NULL).
    assert "LEFT JOIN detector_state" not in sql, f"{sql_name}: JOIN ослаблен до LEFT"


@pytest.mark.parametrize("sql_name, sql", _GATED_SQLS, ids=_GATED_IDS)
def test_money_gate_allows_only_virtual_and_live(sql_name, sql):
    """Из 4 стадий к деньгам допущены ровно virtual+live; paused/shadow — нет."""
    assert "dst.stage IN ('virtual', 'live')" in sql, f"{sql_name}: изменился список допущенных stage"
    # Явная защита от расширения списка: paused/shadow не должны появиться рядом
    # с 'virtual'/'live' внутри условия JOIN.
    join_condition = sql.split("JOIN detector_state dst", 1)[1].split("WHERE", 1)[0]
    assert "'paused'" not in join_condition, f"{sql_name}: paused допущен к деньгам"
    assert "'shadow'" not in join_condition, f"{sql_name}: shadow допущен к деньгам"


def test_both_fetch_paths_share_the_same_gate():
    """Cron-тик и демон-по-id фильтруют одинаково — иначе демон исполнит отброшенное cron'ом."""
    # Оба варианта строятся из общей основы (см. комментарий над _SIGNAL_SELECT_BASE_SQL).
    assert _FETCH_FRESH_SIGNALS_SQL.startswith(_SIGNAL_SELECT_BASE_SQL)
    assert _FETCH_SIGNAL_BY_ID_SQL.startswith(_SIGNAL_SELECT_BASE_SQL)


# ─── PR-D: lifecycle-dropped telemetry (D44 silent black-hole fix) ───────────


def test_dropped_signals_telemetry_sql_filters_correctly():
    """SQL должен LEFT JOIN + COALESCE 'unregistered' + filter NOT IN virtual/live."""
    sql = _DROPPED_SIGNALS_TELEMETRY_SQL
    assert "LEFT JOIN detector_state" in sql
    assert "COALESCE(dst.stage, 'unregistered')" in sql
    # Filter — берём только то, что INNER JOIN бы отбросил
    assert "dst.stage IS NULL OR dst.stage NOT IN ('virtual', 'live')" in sql
    # 6h окно совпадает с _FETCH_FRESH_SIGNALS_SQL hard staleness cap
    assert "interval '6 hours'" in sql


@pytest.mark.asyncio
async def test_log_lifecycle_dropped_signals_logs_each_detector_bucket():
    """Когда DB возвращает rows — logger.info вызывается с total + by_detector."""
    rows = [
        {"detector": "D44_late_convergence_carry", "stage_or_missing": "unregistered", "n_dropped": 172},
        {"detector": "D29_basket_arbitrage", "stage_or_missing": "shadow", "n_dropped": 8},
    ]
    fetch_mock = AsyncMock(return_value=rows)

    with (
        patch("app.services.detector_signal_service.fetch", fetch_mock),
        patch("app.services.detector_signal_service.logger") as logger_mock,
    ):
        await _log_lifecycle_dropped_signals()

    logger_mock.info.assert_called_once()
    args, kwargs = logger_mock.info.call_args
    assert args[0] == "lifecycle_dropped_signals"
    assert kwargs["total"] == 180  # 172 + 8
    assert kwargs["by_detector"] == {
        "D44_late_convergence_carry(unregistered)": 172,
        "D29_basket_arbitrage(shadow)": 8,
    }


@pytest.mark.asyncio
async def test_log_lifecycle_dropped_signals_silent_on_empty():
    """Нет dropped signals — никакого log спама."""
    fetch_mock = AsyncMock(return_value=[])

    with (
        patch("app.services.detector_signal_service.fetch", fetch_mock),
        patch("app.services.detector_signal_service.logger") as logger_mock,
    ):
        await _log_lifecycle_dropped_signals()

    logger_mock.info.assert_not_called()


@pytest.mark.asyncio
async def test_log_lifecycle_dropped_signals_tolerates_db_error():
    """DB error не должен валить fetch_calibrated_detector_signals."""
    fetch_mock = AsyncMock(side_effect=RuntimeError("db down"))

    with (
        patch("app.services.detector_signal_service.fetch", fetch_mock),
        patch("app.services.detector_signal_service.logger") as logger_mock,
    ):
        # Не должен raise
        await _log_lifecycle_dropped_signals()

    logger_mock.warning.assert_called_once()
    args, _ = logger_mock.warning.call_args
    assert args[0] == "lifecycle_dropped_telemetry_failed"


# --- fetch_calibrated_signal_by_id (Phase 3 Task 3, audit топ-7) ----------
# Рефактор _FETCH_FRESH_SIGNALS_SQL на общую основу (_SIGNAL_SELECT_BASE_SQL)
# + вариант по id (_FETCH_SIGNAL_BY_ID_SQL), без дублирования age/cooldown/
# stage-фильтров — см. detector_signal_service.py.


def test_by_id_sql_reuses_base_filters():
    """_FETCH_SIGNAL_BY_ID_SQL строится из той же основы, что и _FETCH_FRESH_SIGNALS_SQL
    (не дублирует age/cooldown/stage условия отдельным текстом)."""
    assert _FETCH_SIGNAL_BY_ID_SQL.startswith(_SIGNAL_SELECT_BASE_SQL)
    assert _FETCH_FRESH_SIGNALS_SQL.startswith(_SIGNAL_SELECT_BASE_SQL)
    assert "and ds.id = $1" in _FETCH_SIGNAL_BY_ID_SQL.lower()
    # Основа несёт cooldown-фильтр (Phase 1) и NOT EXISTS дедуп — оба варианта её видят.
    assert "not exists" in _SIGNAL_SELECT_BASE_SQL.lower()
    assert "is_transient_reject" in _SIGNAL_SELECT_BASE_SQL


@pytest.mark.asyncio
async def test_fetch_by_id_returns_none_when_filtered_out():
    """Сигнал протух/в cooldown/сменил stage → основа его не вернула → None
    (демон молча пропускает, cron-watchdog подхватит при необходимости)."""
    fetch_mock = AsyncMock(return_value=[])

    with patch("app.services.detector_signal_service.fetch", fetch_mock):
        result = await fetch_calibrated_signal_by_id("sig-gone")

    assert result is None
    # by_id (демон) не проставляет lock_timeout — прежнее поведение (None).
    fetch_mock.assert_awaited_once_with(_FETCH_SIGNAL_BY_ID_SQL, "sig-gone", lock_timeout_ms=None)


@pytest.mark.asyncio
async def test_fetch_by_id_returns_calibrated_signal():
    """Найден и калиброван — тот же формат dict, что и list-вариант."""
    signal_id = str(uuid4())
    rows = [_row(id=signal_id, raw_p=0.7)]

    fetch_mock = AsyncMock()
    # 1 fetch call для by-id варианта через сервис: signal (нет lifecycle-телеметрии).
    fetch_mock.side_effect = [rows]
    # Загрузчики калибраторов fetch'ат через fallback_chain: L1 + LGB
    chain_fetch_mock = AsyncMock()
    chain_fetch_mock.side_effect = [[], []]

    with (
        patch("app.services.detector_signal_service.fetch", fetch_mock),
        patch("app.calibration.fallback_chain.fetch", chain_fetch_mock),
        patch(
            "app.services.detector_signal_service.fetchrow",
            new_callable=AsyncMock,
            return_value=None,
        ),
    ):
        result = await fetch_calibrated_signal_by_id(signal_id)

    assert result is not None
    assert result["detector_signal_id"] == signal_id
    assert result["raw_p"] == 0.7
    assert result["calibrated_p"] == 0.7  # cold-start fallback
    assert result["meta_p"] == 0.7
    # Первый fetch-вызов — по SQL с ds.id=$1 и переданным id.
    first_call = fetch_mock.await_args_list[0]
    assert first_call.args[0] == _FETCH_SIGNAL_BY_ID_SQL
    assert first_call.args[1] == signal_id


# ─── Задача 1.1: shadow-режим per-anchor калибраторов ────────────────────────
# _emit_cal_anchor_shadow НЕ должна влиять на торгуемый calibrated_p/meta_p —
# она только эмитит structured-событие cal_anchor_shadow для сравнения anchor_p
# vs боевого flat_p. Ошибка внутри shadow-вызова не должна ломать основной путь.


@pytest.mark.asyncio
async def test_emit_cal_anchor_shadow_emits_event_for_per_anchor_detector():
    """STANDALONE_V2_LGB + evidence.anchor_hours=24 → cal_anchor_shadow с обоими p."""
    row = {"evidence": {"anchor_hours": 24}, "category": "Sports", "current_price": 0.5}

    async def fake_calibrate(raw_p, detector, cals, row=None, lgb_cals=None, anchor_hours=None, settings=None):
        assert anchor_hours == 24  # shadow-вызов обязан передать anchor_hours
        return 0.66, "beta"

    with (
        patch("app.services.detector_signal_service._calibrate", side_effect=fake_calibrate),
        patch("app.services.detector_signal_service.logger") as logger_mock,
    ):
        await _emit_cal_anchor_shadow(
            detector="STANDALONE_V2_LGB",
            signal_id="sig-1",
            market_id="mkt-1",
            raw_p=0.7,
            row=row,
            cals={},
            lgb_cals={},
            settings=None,
            flat_p=0.7,
            flat_source="raw",
        )

    logger_mock.info.assert_called_once()
    args, kwargs = logger_mock.info.call_args
    assert args[0] == "cal_anchor_shadow"
    assert kwargs["detector"] == "STANDALONE_V2_LGB"
    assert kwargs["signal_id"] == "sig-1"
    assert kwargs["market_id"] == "mkt-1"
    assert kwargs["anchor_hours"] == 24
    assert kwargs["flat_p"] == 0.7
    assert kwargs["flat_source"] == "raw"
    assert kwargs["anchor_p"] == 0.66
    assert kwargs["anchor_source"] == "beta"


@pytest.mark.asyncio
async def test_emit_cal_anchor_shadow_skips_non_per_anchor_detector():
    """Детектор вне _PER_ANCHOR_DETECTORS (например D1_fade_whale) — no-op, no calibrate call."""
    row = {"evidence": {"anchor_hours": 24}}

    with (
        patch("app.services.detector_signal_service._calibrate", new_callable=AsyncMock) as cal_mock,
        patch("app.services.detector_signal_service.logger") as logger_mock,
    ):
        await _emit_cal_anchor_shadow(
            detector="D1_fade_whale",
            signal_id="sig-2",
            market_id="mkt-2",
            raw_p=0.7,
            row=row,
            cals={},
            lgb_cals={},
            settings=None,
            flat_p=0.7,
            flat_source="raw",
        )

    cal_mock.assert_not_called()
    logger_mock.info.assert_not_called()
    logger_mock.warning.assert_not_called()


@pytest.mark.asyncio
async def test_emit_cal_anchor_shadow_skips_missing_anchor_hours():
    """Нет evidence.anchor_hours (большинство детекторов) — no-op, тихо пропускаем."""
    row = {"evidence": {"tx_hash": "0xabc"}}

    with (
        patch("app.services.detector_signal_service._calibrate", new_callable=AsyncMock) as cal_mock,
        patch("app.services.detector_signal_service.logger") as logger_mock,
    ):
        await _emit_cal_anchor_shadow(
            detector="STANDALONE_V2_LGB",
            signal_id="sig-3",
            market_id="mkt-3",
            raw_p=0.7,
            row=row,
            cals={},
            lgb_cals={},
            settings=None,
            flat_p=0.7,
            flat_source="raw",
        )

    cal_mock.assert_not_called()
    logger_mock.info.assert_not_called()


@pytest.mark.asyncio
async def test_emit_cal_anchor_shadow_exception_does_not_raise():
    """Исключение внутри shadow calibrate() → warning-лог, не raise (основной путь жив)."""

    async def broken_calibrate(*args, **kwargs):
        raise RuntimeError("synthetic shadow failure")

    row = {"evidence": {"anchor_hours": 72}}

    with (
        patch("app.services.detector_signal_service._calibrate", side_effect=broken_calibrate),
        patch("app.services.detector_signal_service.logger") as logger_mock,
    ):
        # Не должно бросить исключение наружу.
        await _emit_cal_anchor_shadow(
            detector="STANDALONE_V2_LGB",
            signal_id="sig-4",
            market_id="mkt-4",
            raw_p=0.7,
            row=row,
            cals={},
            lgb_cals={},
            settings=None,
            flat_p=0.7,
            flat_source="raw",
        )

    logger_mock.info.assert_not_called()
    logger_mock.warning.assert_called_once()
    args, kwargs = logger_mock.warning.call_args
    assert args[0] == "cal_anchor_shadow_failed"
    assert kwargs["detector"] == "STANDALONE_V2_LGB"
    assert kwargs["signal_id"] == "sig-4"


@pytest.mark.asyncio
async def test_fetch_calibrated_signals_flat_cal_p_unaffected_by_shadow():
    """End-to-end: STANDALONE_V2_LGB сигнал с anchor_hours в evidence — боевой
    calibrated_p/meta_p остаются от вызова с anchor_hours=None, даже когда
    shadow-путь эмитит cal_anchor_shadow параллельно."""
    rows = [
        _row(
            detector="STANDALONE_V2_LGB",
            raw_p=0.7,
            evidence={"anchor_hours": 24},
        )
    ]

    fetch_mock = AsyncMock()
    fetch_mock.side_effect = [rows, []]
    chain_fetch_mock = AsyncMock()
    chain_fetch_mock.side_effect = [[], []]

    call_log: list[dict] = []

    async def spy_calibrate(raw_p, detector, cals, row=None, lgb_cals=None, anchor_hours=None, settings=None):
        call_log.append({"anchor_hours": anchor_hours})
        if anchor_hours is None:
            return raw_p, "raw"  # боевой путь: raw fallback (cold-start)
        return 0.42, "beta"  # shadow путь: другое значение, чтобы отличить

    with (
        patch("app.services.detector_signal_service.fetch", fetch_mock),
        patch("app.calibration.fallback_chain.fetch", chain_fetch_mock),
        patch(
            "app.services.detector_signal_service.fetchrow",
            new_callable=AsyncMock,
            return_value=None,
        ),
        patch("app.services.detector_signal_service._calibrate", side_effect=spy_calibrate),
        patch("app.services.detector_signal_service.logger") as logger_mock,
    ):
        result = await fetch_calibrated_detector_signals()

    assert len(result) == 1
    sig = result[0]
    # Боевой calibrated_p/meta_p — от вызова anchor_hours=None, НЕ от shadow (0.42).
    assert sig["calibrated_p"] == 0.7
    assert sig["meta_p"] == 0.7
    assert sig["cal_source"] == "raw"

    # calibrate() вызван дважды: боевой (anchor_hours=None) + shadow (anchor_hours=24)
    assert call_log == [{"anchor_hours": None}, {"anchor_hours": 24}]

    # shadow-событие эмитировано с anchor_p=0.42, не тронув боевой путь.
    shadow_calls = [c for c in logger_mock.info.call_args_list if c.args[0] == "cal_anchor_shadow"]
    assert len(shadow_calls) == 1
    shadow_kwargs = shadow_calls[0].kwargs
    assert shadow_kwargs["flat_p"] == 0.7
    assert shadow_kwargs["anchor_p"] == 0.42
    assert shadow_kwargs["anchor_hours"] == 24
