"""Tests для two_stage_inference (Stage 4).

Critical scenarios:
- Happy path: detector → L1 → Layer A pass → Layer B → meta_p
- Layer A fail: gate=False, meta_p=calibrated_p
- No Layer A: cold-start, gate=True default
- No Layer B: meta_p=calibrated_p
- Layer A predict failure: gracefully degrades
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from unittest.mock import AsyncMock, patch


def _fake_signal(sig_id: str, event_id: str, raw_p: float = 0.7) -> dict:
    return {
        "id": sig_id,
        "detector": "D1_fade_whale",
        "market_id": f"m_{sig_id}",
        "event_id": event_id,
        "side": "NO",
        "raw_p": raw_p,
        "expected_edge_pct": 10.0,
        "fair_price": 0.5,
        "entry_price": 0.5,
        "horizon_sec": 1800,
        "token_id": "tok1",
        "evidence": {},
        "generated_at": datetime(2026, 5, 1, tzinfo=UTC),
        "event_size": 5,
        "archetype": "single_market",
        "category": "Politics",
        "volume_24h": 10000.0,
        "event_volume_24h": 50000.0,
        "event_liquidity": 5000.0,
        "neg_risk": False,
        "neg_risk_other": False,
        "n_yes_winners": 1,
        "end_date": datetime(2026, 5, 8, tzinfo=UTC),
        "n_markets": 5,
        "mean_market_price": 0.5,
        "market_price_dispersion": 0.05,
        "n_detector_signals_event": 3,
        "days_to_resolution": 7.0,
    }


def test_happy_path_layer_b_predicts_meta_p():
    """Layer A passes gate (p_event=0.7), Layer B возвращает meta_p=0.65."""
    from app.services.two_stage_inference import fetch_two_stage_calibrated_signals

    fake = _fake_signal("sig1", "ev1")

    fake_layer_a = AsyncMock()
    fake_layer_a.predict = lambda examples: [0.7]  # > 0.50 threshold
    fake_layer_b = AsyncMock()
    fake_layer_b.predict = lambda examples: [0.65]

    with (
        patch("app.services.two_stage_inference._fetch_fresh_signals", new=AsyncMock(return_value=[fake])),
        patch("app.services.two_stage_inference._load_l1_calibrators", new=AsyncMock(return_value={})),
        patch("app.services.two_stage_inference._load_active_layer_a", new=AsyncMock(return_value=fake_layer_a)),
        patch(
            "app.services.two_stage_inference._load_active_layer_b",
            new=AsyncMock(return_value=(fake_layer_b, 0.50)),
        ),
    ):
        results = asyncio.run(fetch_two_stage_calibrated_signals())

    assert len(results) == 1
    assert results[0]["p_event"] == 0.7
    assert results[0]["passed_layer_a_gate"] is True
    assert results[0]["meta_p"] == 0.65


def test_layer_a_gate_fails_meta_p_falls_back_to_calibrated():
    """p_event=0.3 < threshold → gate=False, meta_p=calibrated_p (Layer B not called)."""
    from app.services.two_stage_inference import fetch_two_stage_calibrated_signals

    fake = _fake_signal("sig2", "ev2")

    fake_layer_a = AsyncMock()
    fake_layer_a.predict = lambda examples: [0.3]  # < 0.50

    fake_layer_b = AsyncMock()
    fake_layer_b.predict = lambda examples: [0.99]  # should NOT be used

    with (
        patch("app.services.two_stage_inference._fetch_fresh_signals", new=AsyncMock(return_value=[fake])),
        patch("app.services.two_stage_inference._load_l1_calibrators", new=AsyncMock(return_value={})),
        patch("app.services.two_stage_inference._load_active_layer_a", new=AsyncMock(return_value=fake_layer_a)),
        patch(
            "app.services.two_stage_inference._load_active_layer_b",
            new=AsyncMock(return_value=(fake_layer_b, 0.50)),
        ),
    ):
        results = asyncio.run(fetch_two_stage_calibrated_signals())

    assert results[0]["p_event"] == 0.3
    assert results[0]["passed_layer_a_gate"] is False
    # meta_p = calibrated_p (no L1 → raw_p = 0.7)
    assert results[0]["meta_p"] == results[0]["calibrated_p"]


def test_no_layer_a_cold_start():
    """No active Layer A → all signals pass with meta_p=calibrated_p (cold-start)."""
    from app.services.two_stage_inference import fetch_two_stage_calibrated_signals

    fake = _fake_signal("sig3", "ev3", raw_p=0.6)

    with (
        patch("app.services.two_stage_inference._fetch_fresh_signals", new=AsyncMock(return_value=[fake])),
        patch("app.services.two_stage_inference._load_l1_calibrators", new=AsyncMock(return_value={})),
        patch("app.services.two_stage_inference._load_active_layer_a", new=AsyncMock(return_value=None)),
        patch(
            "app.services.two_stage_inference._load_active_layer_b",
            new=AsyncMock(return_value=(None, 0.50)),
        ),
    ):
        results = asyncio.run(fetch_two_stage_calibrated_signals())

    assert results[0]["p_event"] is None
    assert results[0]["passed_layer_a_gate"] is True  # default true
    assert results[0]["meta_p"] == results[0]["calibrated_p"]


def test_no_layer_b_meta_p_equals_calibrated():
    """Layer A passes но no Layer B → meta_p=calibrated_p."""
    from app.services.two_stage_inference import fetch_two_stage_calibrated_signals

    fake = _fake_signal("sig4", "ev4")
    fake_layer_a = AsyncMock()
    fake_layer_a.predict = lambda examples: [0.8]

    with (
        patch("app.services.two_stage_inference._fetch_fresh_signals", new=AsyncMock(return_value=[fake])),
        patch("app.services.two_stage_inference._load_l1_calibrators", new=AsyncMock(return_value={})),
        patch("app.services.two_stage_inference._load_active_layer_a", new=AsyncMock(return_value=fake_layer_a)),
        patch(
            "app.services.two_stage_inference._load_active_layer_b",
            new=AsyncMock(return_value=(None, 0.50)),
        ),
    ):
        results = asyncio.run(fetch_two_stage_calibrated_signals())

    assert results[0]["p_event"] == 0.8
    assert results[0]["passed_layer_a_gate"] is True
    assert results[0]["meta_p"] == results[0]["calibrated_p"]


def test_layer_a_predict_exception_graceful_degradation():
    """Если Layer A.predict() падает → p_event=None, gate=True (cold-start fallback)."""
    from app.services.two_stage_inference import fetch_two_stage_calibrated_signals

    fake = _fake_signal("sig5", "ev5")
    broken_layer_a = AsyncMock()

    def _explode(examples):
        raise RuntimeError("synthetic broken model")

    broken_layer_a.predict = _explode

    with (
        patch("app.services.two_stage_inference._fetch_fresh_signals", new=AsyncMock(return_value=[fake])),
        patch("app.services.two_stage_inference._load_l1_calibrators", new=AsyncMock(return_value={})),
        patch("app.services.two_stage_inference._load_active_layer_a", new=AsyncMock(return_value=broken_layer_a)),
        patch(
            "app.services.two_stage_inference._load_active_layer_b",
            new=AsyncMock(return_value=(None, 0.50)),
        ),
    ):
        results = asyncio.run(fetch_two_stage_calibrated_signals())

    assert results[0]["p_event"] is None
    assert results[0]["passed_layer_a_gate"] is True


def test_empty_input_returns_empty():
    from app.services.two_stage_inference import fetch_two_stage_calibrated_signals

    with patch("app.services.two_stage_inference._fetch_fresh_signals", new=AsyncMock(return_value=[])):
        results = asyncio.run(fetch_two_stage_calibrated_signals())
    assert results == []


def test_required_keys_in_output():
    from app.services.two_stage_inference import fetch_two_stage_calibrated_signals

    fake = _fake_signal("sig6", "ev6")
    fake_layer_a = AsyncMock()
    fake_layer_a.predict = lambda examples: [0.6]

    with (
        patch("app.services.two_stage_inference._fetch_fresh_signals", new=AsyncMock(return_value=[fake])),
        patch("app.services.two_stage_inference._load_l1_calibrators", new=AsyncMock(return_value={})),
        patch("app.services.two_stage_inference._load_active_layer_a", new=AsyncMock(return_value=fake_layer_a)),
        patch(
            "app.services.two_stage_inference._load_active_layer_b",
            new=AsyncMock(return_value=(None, 0.50)),
        ),
    ):
        results = asyncio.run(fetch_two_stage_calibrated_signals())

    sig = results[0]
    required = {
        "detector_signal_id", "detector", "market_id", "event_id", "side",
        "raw_p", "calibrated_p", "meta_p", "p_event", "passed_layer_a_gate",
        "entry_price", "expected_edge_pct", "horizon_sec", "evidence",
        "generated_at", "category", "volume_24h", "token_id",
    }
    assert required.issubset(sig.keys())


def test_g18_bin_calibration_source_no_keyerror():
    """Regression for prod incident 2026-05-26T13:09Z (PR #503 deploy):
    Когда `_calibrate` возвращает source='g18_bin', `cal_source_counts` инкрементится.
    Если ключ не инициализирован → KeyError → cron job упадёт каждые 10 минут.

    Это тест ловит exactly the bug, который убил pipeline-v2-shadow-predictor.
    """
    from app.services.two_stage_inference import fetch_two_stage_calibrated_signals

    fake = _fake_signal("sig_g18", "ev_g18")
    fake_layer_a = AsyncMock()
    fake_layer_a.predict = lambda examples: [0.6]

    async def mock_calibrate(raw_p, detector, cals, **kwargs):
        return raw_p, "g18_bin"  # ← simulating G18 tier hit

    with (
        patch("app.services.two_stage_inference._fetch_fresh_signals", new=AsyncMock(return_value=[fake])),
        patch("app.services.two_stage_inference._load_l1_calibrators", new=AsyncMock(return_value={})),
        patch("app.services.two_stage_inference._load_active_layer_a", new=AsyncMock(return_value=fake_layer_a)),
        patch(
            "app.services.two_stage_inference._load_active_layer_b",
            new=AsyncMock(return_value=(None, 0.50)),
        ),
        patch("app.services.two_stage_inference._calibrate", side_effect=mock_calibrate),
    ):
        # No KeyError должен полететь
        results = asyncio.run(fetch_two_stage_calibrated_signals())

    assert len(results) == 1
    assert results[0]["cal_source"] == "g18_bin"


# ─── Fix-3 (latency-аудит №3): паритет с monolith по price-telemetry полям ────
# Контракт fetch_signals_for_active_pipeline: оба пути возвращают одинаковые
# ключи. current_price/yes_token_id/fair_price нужны sizing-телеметрии
# (v2_execution._sizing_current_price) и TG-нотификации live-ордера.


def test_fetch_sql_selects_yes_token_id():
    """SQL two-stage должен отдавать YES-токен для get_live_yes_price."""
    from app.services.two_stage_inference import _FETCH_FRESH_SIGNALS_SQL

    assert "AS yes_token_id" in _FETCH_FRESH_SIGNALS_SQL


def test_enriched_propagates_price_telemetry_fields():
    """current_price/yes_token_id/fair_price доходят до sig-словаря (как в monolith)."""
    from app.services.two_stage_inference import fetch_two_stage_calibrated_signals

    fake = _fake_signal("sig5", "ev5")
    fake["current_price"] = 0.55
    fake["yes_token_id"] = "tok-yes"

    with (
        patch("app.services.two_stage_inference._fetch_fresh_signals", new=AsyncMock(return_value=[fake])),
        patch("app.services.two_stage_inference._load_l1_calibrators", new=AsyncMock(return_value={})),
        patch("app.services.two_stage_inference._load_active_layer_a", new=AsyncMock(return_value=None)),
        patch(
            "app.services.two_stage_inference._load_active_layer_b",
            new=AsyncMock(return_value=(None, 0.50)),
        ),
    ):
        results = asyncio.run(fetch_two_stage_calibrated_signals())

    sig = results[0]
    assert sig["current_price"] == 0.55
    assert sig["yes_token_id"] == "tok-yes"
    assert sig["fair_price"] == 0.5  # из _fake_signal


# ─── Задача 1.1: shadow-режим per-anchor калибраторов ────────────────────────
# _emit_cal_anchor_shadow (two-stage путь) — то же поведение, что и в
# detector_signal_service: не влияет на calibrated_p/meta_p, только логирует.


def test_emit_cal_anchor_shadow_emits_event_for_per_anchor_detector():
    from app.services.two_stage_inference import _emit_cal_anchor_shadow

    row = {"evidence": {"anchor_hours": 24}, "category": "Sports"}

    async def fake_calibrate(raw_p, detector, cals, row=None, lgb_cals=None, anchor_hours=None, settings=None):
        assert anchor_hours == 24
        return 0.55, "lgb"

    async def _run():
        with (
            patch("app.services.two_stage_inference._calibrate", side_effect=fake_calibrate),
            patch("app.services.two_stage_inference.logger") as logger_mock,
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
        return logger_mock

    logger_mock = asyncio.run(_run())
    logger_mock.info.assert_called_once()
    args, kwargs = logger_mock.info.call_args
    assert args[0] == "cal_anchor_shadow"
    assert kwargs["anchor_hours"] == 24
    assert kwargs["flat_p"] == 0.7
    assert kwargs["anchor_p"] == 0.55
    assert kwargs["anchor_source"] == "lgb"


def test_emit_cal_anchor_shadow_skips_non_per_anchor_detector():
    from app.services.two_stage_inference import _emit_cal_anchor_shadow

    row = {"evidence": {"anchor_hours": 24}}

    async def _run():
        with (
            patch("app.services.two_stage_inference._calibrate", new_callable=AsyncMock) as cal_mock,
            patch("app.services.two_stage_inference.logger") as logger_mock,
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
        return cal_mock, logger_mock

    cal_mock, logger_mock = asyncio.run(_run())
    cal_mock.assert_not_called()
    logger_mock.info.assert_not_called()


def test_emit_cal_anchor_shadow_exception_does_not_raise():
    from app.services.two_stage_inference import _emit_cal_anchor_shadow

    async def broken_calibrate(*args, **kwargs):
        raise RuntimeError("synthetic shadow failure")

    row = {"evidence": {"anchor_hours": 72}}

    async def _run():
        with (
            patch("app.services.two_stage_inference._calibrate", side_effect=broken_calibrate),
            patch("app.services.two_stage_inference.logger") as logger_mock,
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
        return logger_mock

    logger_mock = asyncio.run(_run())
    logger_mock.info.assert_not_called()
    logger_mock.warning.assert_called_once()
    args, kwargs = logger_mock.warning.call_args
    assert args[0] == "cal_anchor_shadow_failed"


def test_fetch_two_stage_flat_cal_p_unaffected_by_shadow():
    """End-to-end: боевой calibrated_p/meta_p не меняется shadow-вызовом даже
    когда evidence.anchor_hours присутствует для STANDALONE_V2_LGB."""
    from app.services.two_stage_inference import fetch_two_stage_calibrated_signals

    fake = _fake_signal("sig7", "ev7", raw_p=0.7)
    fake["detector"] = "STANDALONE_V2_LGB"
    fake["evidence"] = {"anchor_hours": 24}

    call_log: list[dict] = []

    async def spy_calibrate(raw_p, detector, cals, row=None, lgb_cals=None, anchor_hours=None, settings=None):
        call_log.append({"anchor_hours": anchor_hours})
        if anchor_hours is None:
            return raw_p, "raw"
        return 0.11, "beta"

    with (
        patch("app.services.two_stage_inference._fetch_fresh_signals", new=AsyncMock(return_value=[fake])),
        patch("app.services.two_stage_inference._load_l1_calibrators", new=AsyncMock(return_value={})),
        patch("app.services.two_stage_inference._load_active_layer_a", new=AsyncMock(return_value=None)),
        patch(
            "app.services.two_stage_inference._load_active_layer_b",
            new=AsyncMock(return_value=(None, 0.50)),
        ),
        patch("app.services.two_stage_inference._calibrate", side_effect=spy_calibrate),
        patch("app.services.two_stage_inference.logger") as logger_mock,
    ):
        results = asyncio.run(fetch_two_stage_calibrated_signals())

    sig = results[0]
    assert sig["calibrated_p"] == 0.7
    assert sig["meta_p"] == 0.7
    assert sig["cal_source"] == "raw"
    assert call_log == [{"anchor_hours": None}, {"anchor_hours": 24}]

    shadow_calls = [c for c in logger_mock.info.call_args_list if c.args[0] == "cal_anchor_shadow"]
    assert len(shadow_calls) == 1
    assert shadow_calls[0].kwargs["flat_p"] == 0.7
    assert shadow_calls[0].kwargs["anchor_p"] == 0.11
