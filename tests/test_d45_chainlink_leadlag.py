"""Tests для D45 detector — pure functions, no I/O."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from app.detectors.d45_chainlink_leadlag import (
    DETECTOR_NAME,
    MarketState,
    PythSnapshot,
    detect,
    parse_slug,
    to_signal,
)


def _make_market(
    *,
    yes_midpoint: float = 0.5,
    window_end_ts: int = 1779360000,
    horizon_sec: int = 300,
    slug: str = "btc-updown-5m-1779359700",
    clob_token_ids: list[str] | None = None,
) -> MarketState:
    return MarketState(
        market_id="m-1",
        condition_id="0xabc",
        slug=slug,
        yes_midpoint=yes_midpoint,
        window_start_ts=window_end_ts - horizon_sec,
        window_end_ts=window_end_ts,
        horizon_sec=horizon_sec,
        event_id="e-1",
        token_id="t-yes",
        clob_token_ids=clob_token_ids if clob_token_ids is not None else ["t-yes", "t-no"],
    )


def _make_pyth(
    *,
    symbol: str = "BTC/USD",
    price_now: float = 77000.0,
    price_at_open: float | None = 77000.0,
    publish_time_ms: int = 0,
) -> PythSnapshot:
    return PythSnapshot(
        symbol=symbol,
        price_now=price_now,
        price_at_window_open=price_at_open,
        publish_time_ms=publish_time_ms,
    )


class TestTimingWindow:
    """detect возвращает None если now вне резолюционного окна."""

    def test_too_early_returns_none(self):
        """Окно закроется через 60s — слишком рано (max=30s)."""
        result = detect(
            _make_market(window_end_ts=1779360000),
            _make_pyth(price_now=77500.0),
            now_ts=1779359940,  # 60s до конца
        )
        assert result is None

    def test_too_late_returns_none(self):
        """Окно закроется через 3s — слишком поздно (min=5s)."""
        result = detect(
            _make_market(window_end_ts=1779360000),
            _make_pyth(price_now=77500.0),
            now_ts=1779359997,
        )
        assert result is None

    def test_after_end_returns_none(self):
        """Окно уже закрылось — None."""
        result = detect(
            _make_market(window_end_ts=1779360000),
            _make_pyth(price_now=77500.0),
            now_ts=1779360001,
        )
        assert result is None

    def test_in_window_returns_detection(self):
        """20s до конца окна, divergence есть → возвращает Detection."""
        result = detect(
            _make_market(yes_midpoint=0.5, window_end_ts=1779360000),
            _make_pyth(price_now=77500.0, price_at_open=77000.0),  # +0.65%
            now_ts=1779359980,
        )
        assert result is not None
        assert result.seconds_to_resolution == 20


class TestPythAvailability:
    def test_no_window_open_price(self):
        result = detect(
            _make_market(),
            _make_pyth(price_at_open=None),
            now_ts=1779359980,
        )
        assert result is None

    def test_zero_window_open_price(self):
        """price_at_open=0 — divide-by-zero guard."""
        result = detect(
            _make_market(),
            _make_pyth(price_at_open=0.0),
            now_ts=1779359980,
        )
        assert result is None

    def test_negative_price_now(self):
        result = detect(
            _make_market(),
            _make_pyth(price_now=-1.0, price_at_open=77000.0),
            now_ts=1779359980,
        )
        assert result is None


class TestMarketSanity:
    def test_yes_midpoint_zero(self):
        result = detect(
            _make_market(yes_midpoint=0.0),
            _make_pyth(price_now=77500.0, price_at_open=77000.0),
            now_ts=1779359980,
        )
        assert result is None

    def test_yes_midpoint_one(self):
        result = detect(
            _make_market(yes_midpoint=1.0),
            _make_pyth(price_now=77500.0, price_at_open=77000.0),
            now_ts=1779359980,
        )
        assert result is None

    def test_yes_midpoint_negative(self):
        result = detect(
            _make_market(yes_midpoint=-0.1),
            _make_pyth(price_now=77500.0, price_at_open=77000.0),
            now_ts=1779359980,
        )
        assert result is None


class TestPythBinanceBasisCheck:
    """Pyth vs Binance cross-source sanity check (защита от single-source glitch)."""

    def test_binance_agrees_signal_proceeds(self):
        """Pyth $77500, Binance $77490 → basis 0.013%, ниже threshold 0.5%."""
        result = detect(
            _make_market(yes_midpoint=0.5),
            _make_pyth(price_now=77500.0, price_at_open=77000.0),
            now_ts=1779359980,
            binance_price_now=77490.0,
        )
        assert result is not None
        assert result.side == "YES"

    def test_binance_disagrees_signal_skipped(self):
        """Pyth $77500, Binance $76000 → basis 1.97%, выше threshold 0.5%."""
        result = detect(
            _make_market(yes_midpoint=0.5),
            _make_pyth(price_now=77500.0, price_at_open=77000.0),
            now_ts=1779359980,
            binance_price_now=76000.0,
        )
        assert result is None  # Pyth glitch suspect → skip

    def test_binance_none_skips_check(self):
        """binance_price_now=None → cross-check выключен (legacy)."""
        result = detect(
            _make_market(yes_midpoint=0.5),
            _make_pyth(price_now=77500.0, price_at_open=77000.0),
            now_ts=1779359980,
            binance_price_now=None,
        )
        assert result is not None

    def test_binance_zero_or_negative_skips_check(self):
        """Невалидный binance_price (≤0) → skip check, не блокирует signal."""
        result = detect(
            _make_market(yes_midpoint=0.5),
            _make_pyth(price_now=77500.0, price_at_open=77000.0),
            now_ts=1779359980,
            binance_price_now=0.0,
        )
        assert result is not None

    def test_custom_basis_threshold(self):
        """Caller может ужесточить threshold (например, 0.1% для live mode)."""
        # Binance $77400 → basis = 0.129% > 0.1%
        result = detect(
            _make_market(yes_midpoint=0.5),
            _make_pyth(price_now=77500.0, price_at_open=77000.0),
            now_ts=1779359980,
            binance_price_now=77400.0,
            max_pyth_binance_basis_pct=0.1,
        )
        assert result is None


class TestDivergenceLogic:
    """Главная логика: Pyth move vs CLOB implied move."""

    def test_pyth_up_clob_flat_signals_yes(self):
        """Pyth +0.65%, CLOB 0.50 (no expectation) → divergence positive → YES."""
        result = detect(
            _make_market(yes_midpoint=0.5),
            _make_pyth(price_now=77500.0, price_at_open=77000.0),
            now_ts=1779359980,
        )
        assert result is not None
        assert result.side == "YES"
        assert result.pyth_move_pct == pytest.approx(0.6494, abs=0.001)
        assert result.clob_implied_move_pct == 0.0
        assert result.divergence_pct == pytest.approx(0.6494, abs=0.001)

    def test_pyth_down_clob_flat_signals_no(self):
        """Pyth -0.65%, CLOB 0.50 → divergence negative → NO."""
        result = detect(
            _make_market(yes_midpoint=0.5),
            _make_pyth(price_now=76500.0, price_at_open=77000.0),
            now_ts=1779359980,
        )
        assert result is not None
        assert result.side == "NO"
        assert result.divergence_pct < 0

    def test_both_agree_no_signal(self):
        """Pyth +1%, CLOB 0.55 (implied +10pp) → divergence ~ -9pp,
        |divergence| > threshold но направление противоречит → всё равно signal NO."""
        result = detect(
            _make_market(yes_midpoint=0.55),
            _make_pyth(price_now=77770.0, price_at_open=77000.0),  # +1%
            now_ts=1779359980,
        )
        # CLOB сильнее верит в UP чем Pyth подтверждает → mean revert signal NO
        assert result is not None
        assert result.side == "NO"

    def test_below_threshold_returns_none(self):
        """Pyth +0.1%, CLOB 0.50 → divergence 0.1pp < default 0.3pp → None."""
        result = detect(
            _make_market(yes_midpoint=0.5),
            _make_pyth(price_now=77077.0, price_at_open=77000.0),
            now_ts=1779359980,
        )
        assert result is None

    def test_custom_threshold(self):
        """Тот же 0.1% divergence пройдёт с threshold=0.05."""
        result = detect(
            _make_market(yes_midpoint=0.5),
            _make_pyth(price_now=77077.0, price_at_open=77000.0),
            now_ts=1779359980,
            min_divergence_pct=0.05,
        )
        assert result is not None
        assert result.side == "YES"


class TestConfidence:
    def test_low_divergence_low_confidence(self):
        """Divergence 0.5% → confidence 0.5/5 = 0.10."""
        result = detect(
            _make_market(yes_midpoint=0.5),
            _make_pyth(price_now=77385.0, price_at_open=77000.0),  # +0.5%
            now_ts=1779359980,
        )
        assert result is not None
        assert result.confidence == pytest.approx(0.1, abs=0.01)

    def test_high_divergence_caps_confidence(self):
        """Divergence 10% → cap 0.95."""
        result = detect(
            _make_market(yes_midpoint=0.5),
            _make_pyth(price_now=84700.0, price_at_open=77000.0),  # +10%
            now_ts=1779359980,
        )
        assert result is not None
        assert result.confidence == 0.95


class TestToSignal:
    """to_signal converter в общий Signal contract."""

    def test_basic_yes_signal(self):
        market = _make_market(yes_midpoint=0.5)
        pyth = _make_pyth(price_now=77500.0, price_at_open=77000.0)
        det = detect(market, pyth, now_ts=1779359980)
        assert det is not None
        sig = to_signal(det, market, pyth, generated_at=datetime(2026, 5, 21, tzinfo=UTC))
        assert sig.detector == DETECTOR_NAME
        assert sig.side == "YES"
        assert sig.market_id == "m-1"
        assert sig.confidence == det.confidence
        assert sig.current_price == 0.5
        # fair_price = 0.5 + 0.6494/40 = 0.5162
        assert sig.fair_price == pytest.approx(0.5162, abs=0.001)
        assert sig.horizon_sec == 20
        assert sig.evidence["pyth_symbol"] == "BTC/USD"
        assert "divergence_pct" in sig.evidence

    def test_yes_signal_uses_yes_token(self):
        """side=YES → token_id = YES-токен (index 0)."""
        market = _make_market(yes_midpoint=0.5, clob_token_ids=["tok_yes", "tok_no"])
        pyth = _make_pyth(price_now=77500.0, price_at_open=77000.0)  # Pyth UP → YES
        det = detect(market, pyth, now_ts=1779359980)
        assert det is not None and det.side == "YES"
        sig = to_signal(det, market, pyth)
        assert sig.token_id == "tok_yes"

    def test_no_signal_uses_no_token_not_yes(self):
        """side=NO → token_id обязан быть NO-токеном (index 1), не YES.

        Регрессия: scan хардкодил side_hint='YES' → все NO-сигналы несли YES-токен.
        В live (или при промоуте из shadow) бот купил бы YES при намерении NO —
        ставка против сигнала. Тот же класс бага, что был у D3.
        """
        market = _make_market(yes_midpoint=0.5, clob_token_ids=["tok_yes", "tok_no"])
        pyth = _make_pyth(price_now=76500.0, price_at_open=77000.0)  # Pyth DOWN → NO
        det = detect(market, pyth, now_ts=1779359980)
        assert det is not None and det.side == "NO"
        sig = to_signal(det, market, pyth)
        assert sig.side == "NO"
        assert sig.token_id == "tok_no"

    def test_fair_price_clamped(self):
        """Pyth +50% → fair_price = 0.5 + 50/40 = 1.75 → clamp 0.95."""
        market = _make_market(yes_midpoint=0.5)
        pyth = _make_pyth(price_now=115500.0, price_at_open=77000.0)  # +50%
        det = detect(market, pyth, now_ts=1779359980)
        assert det is not None
        sig = to_signal(det, market, pyth)
        assert sig.fair_price == 0.95

    def test_evidence_includes_pyth_lag(self):
        det = detect(
            _make_market(yes_midpoint=0.5),
            _make_pyth(price_now=77500.0, price_at_open=77000.0),
            now_ts=1779359980,
        )
        assert det is not None
        sig = to_signal(
            det,
            _make_market(yes_midpoint=0.5),
            _make_pyth(price_now=77500.0, price_at_open=77000.0),
            pyth_publish_lag_ms=1234,
        )
        assert sig.evidence["pyth_publish_lag_ms"] == 1234


class TestParseSlug:
    def test_btc_5m(self):
        assert parse_slug("btc-updown-5m-1779354900") == ("btc", 300, 1779354900)

    def test_eth_15m(self):
        assert parse_slug("eth-updown-15m-1779354900") == ("eth", 900, 1779354900)

    def test_sol_4h(self):
        assert parse_slug("sol-updown-4h-1779336000") == ("sol", 14400, 1779336000)

    def test_invalid_returns_none(self):
        assert parse_slug("trump-second-term-2026") is None
        assert parse_slug("") is None
        assert parse_slug("btc-updown") is None
        assert parse_slug("btc-updown-zzm-1779354900") is None
        assert parse_slug("btc-updown-5m-notanumber") is None

    def test_uppercase_normalized(self):
        assert parse_slug("BTC-UPDOWN-5m-1779354900") == ("btc", 300, 1779354900)


class TestRealWorldScenario:
    """End-to-end сценарий с реальными данными (BTC 5m market)."""

    def test_real_btc_window_with_strong_pyth_move(self):
        """BTC резолвится через 12s, Pyth показал +1.2% от open ($77000 → $77924),
        CLOB ещё на 0.55 (implied +10pp). Divergence: 1.2 - 10 = -8.8pp → NO."""
        market = MarketState(
            market_id="m-real",
            condition_id="0xreal",
            slug="btc-updown-5m-1779360000",
            yes_midpoint=0.55,
            window_start_ts=1779359700,
            window_end_ts=1779360000,
            horizon_sec=300,
            event_id="e-real",
            token_id="t-yes-real",
        )
        pyth = PythSnapshot(
            symbol="BTC/USD",
            price_now=77924.0,
            price_at_window_open=77000.0,
            publish_time_ms=1779359988_000,
        )
        det = detect(market, pyth, now_ts=1779359988)
        assert det is not None
        assert det.side == "NO"
        assert det.pyth_move_pct == pytest.approx(1.2, abs=0.01)
        assert det.clob_implied_move_pct == pytest.approx(10.0, abs=0.01)
        assert det.divergence_pct == pytest.approx(-8.8, abs=0.05)
        assert det.confidence == 0.95  # capped
