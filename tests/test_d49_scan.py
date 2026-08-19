"""D49 StrikeLadder — unit tests для build_arb (app/detectors/d49_strike_ladder.py)
и run_d49_scan (app/jobs/d49_scan.py, I/O через моки).
"""

from __future__ import annotations

import time
from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock, patch

import pytest

from app.detectors.d49_strike_ladder import LadderLeg, build_arb


def _leg(market_id: str, strike: float, price: float, *, category: str = "Crypto", token_id: str | None = None) -> LadderLeg:
    return LadderLeg(market_id=market_id, strike=strike, price=price, category=category, token_id=token_id or f"tok-{market_id}")


# ─── build_arb (pure detector logic) ─────────────────────────────────────────


class TestBuildArb:
    def test_violation_emits_two_signals_with_shared_pair_key(self):
        """p2 (0.30) >> p1 (0.02) на K1<K2 — явное нарушение монотонности, net-профитно."""
        leg_k1 = _leg("0x1", 60_000, 0.02)
        leg_k2 = _leg("0x2", 62_000, 0.30)
        result = build_arb(entity="BTC", end_date_key="2026-07-10", window_key="w", leg_k1=leg_k1, leg_k2=leg_k2)
        assert result is not None
        sig_yes, sig_no = result

        assert sig_yes.detector == "D49_strike_ladder"
        assert sig_yes.side == "YES"
        assert sig_yes.market_id == "0x1"
        assert sig_yes.current_price == pytest.approx(0.02)
        assert sig_yes.fair_price == pytest.approx(0.30)

        assert sig_no.side == "NO"
        assert sig_no.market_id == "0x2"
        # Contract (см. d3/d26/d29/d38): current_price/fair_price для NO-стороны
        # хранятся в СЫРОМ YES-номинале рынка — resolve сам инвертирует через
        # _entry_price_for_side (1 - current_price). current_price=1-p2 здесь было бы
        # двойной инверсией (entry=p2 вместо правильного 1-p2) — регрессия против
        # повторения (Opus-ревью NEEDS_FIXES 2026-07-10).
        assert sig_no.current_price == pytest.approx(0.30)  # p2, НЕ 1-p2
        assert sig_no.fair_price == pytest.approx(0.02)  # p1, НЕ 1-p1

        assert sig_yes.evidence["pair_key"] == sig_no.evidence["pair_key"] == "BTC:2026-07-10:w:60000:62000"
        assert sig_yes.evidence["leg"] == "yes_low"
        assert sig_no.evidence["leg"] == "no_high"
        assert sig_yes.expected_edge_pct == sig_no.expected_edge_pct
        assert sig_yes.confidence == sig_no.confidence

    def test_no_leg_current_price_survives_resolver_entry_inversion(self):
        """Сквозная регрессия: прогоняем sig_no.current_price через реальный
        _entry_price_for_side из app/jobs/detector_signals_resolve.py и проверяем,
        что итоговый entry = 1-p2 (правильная цена покупки NO), а не p2 (двойная
        инверсия, была бы если current_price хранился как 1-p2)."""
        from app.jobs.detector_signals_resolve import _entry_price_for_side

        leg_k1 = _leg("0x1", 60_000, 0.10)
        leg_k2 = _leg("0x2", 62_000, 0.30)
        result = build_arb(entity="BTC", end_date_key="d", window_key="w", leg_k1=leg_k1, leg_k2=leg_k2)
        assert result is not None
        _sig_yes, sig_no = result

        entry_no = _entry_price_for_side(sig_no.current_price, sig_no.side)
        assert entry_no == pytest.approx(0.70)  # 1 - p2 = правильная стоимость BUY NO(K2)

    def test_no_violation_monotonic_prices_returns_none(self):
        """p1 >= p2 (нормальная монотонная лесенка) — нет арбитража."""
        leg_k1 = _leg("0x1", 60_000, 0.60)
        leg_k2 = _leg("0x2", 62_000, 0.40)
        assert build_arb(entity="BTC", end_date_key="d", window_key="w", leg_k1=leg_k1, leg_k2=leg_k2) is None

    def test_small_spread_below_buffer_rejected(self):
        """Нарушение есть (p2 чуть выше p1), но net-спред меньше fee+slippage buffer."""
        leg_k1 = _leg("0x1", 60_000, 0.50)
        leg_k2 = _leg("0x2", 62_000, 0.505)  # 0.5pp raw spread, buffer обычно >3pp
        assert build_arb(entity="BTC", end_date_key="d", window_key="w", leg_k1=leg_k1, leg_k2=leg_k2) is None

    def test_confidence_scales_with_net_spread(self):
        """Больше net_spread_pp -> выше confidence, cap 0.95."""
        leg_k1 = _leg("0x1", 60_000, 0.01)
        leg_k2_small = _leg("0x2", 62_000, 0.10)
        leg_k2_big = _leg("0x3", 64_000, 0.90)

        small = build_arb(entity="BTC", end_date_key="d", window_key="w", leg_k1=leg_k1, leg_k2=leg_k2_small)
        big = build_arb(entity="BTC", end_date_key="d", window_key="w", leg_k1=leg_k1, leg_k2=leg_k2_big)
        assert small is not None and big is not None
        assert small[0].confidence < big[0].confidence
        assert big[0].confidence <= 0.95

    def test_invalid_strike_order_returns_none(self):
        leg_k1 = _leg("0x1", 62_000, 0.02)
        leg_k2 = _leg("0x2", 60_000, 0.30)  # K1 > K2 нарушает вызывающий контракт
        assert build_arb(entity="BTC", end_date_key="d", window_key="w", leg_k1=leg_k1, leg_k2=leg_k2) is None

    def test_prices_outside_open_interval_returns_none(self):
        leg_k1 = _leg("0x1", 60_000, 0.0)
        leg_k2 = _leg("0x2", 62_000, 0.5)
        assert build_arb(entity="BTC", end_date_key="d", window_key="w", leg_k1=leg_k1, leg_k2=leg_k2) is None


# ─── run_d49_scan (I/O layer, моки) ──────────────────────────────────────────


def _row(market_id: str, question: str, price: float, end_date):
    return {
        "market_id": market_id,
        "condition_id": f"cond-{market_id}",
        "category": "Crypto",
        "question": question,
        "current_price": price,
        "end_date": end_date,
        "clob_token_ids": [f"yes_{market_id}", f"no_{market_id}"],
        "volume_24h": 10_000.0,
    }


def _no_live_cache():
    """Нет WS-кэша — db-цена используется как есть (fail-closed fallback)."""
    return AsyncMock(return_value=None)


@pytest.mark.asyncio
async def test_scan_violation_yields_two_signals_same_pair_key():
    end_date = datetime.now(UTC) + timedelta(hours=6)
    rows = [
        _row("0x1", "Will BTC be above $60,000 on July 10?", 0.02, end_date),
        _row("0x2", "Will BTC be above $62,000 on July 10?", 0.30, end_date),
    ]
    inserted = []

    async def _fake_insert(signal):
        inserted.append(signal)
        return True

    repo_mock = AsyncMock()
    repo_mock.insert = AsyncMock(side_effect=_fake_insert)

    with (
        patch("app.jobs.d49_scan.fetch", AsyncMock(return_value=rows)),
        patch("app.jobs.d49_scan.get_detector_signal_repository", return_value=repo_mock),
        patch("app.jobs.d49_scan.fetch_live_book", AsyncMock(return_value=None)),
        patch("app.services.live_prices.cache_get", _no_live_cache()),
    ):
        from app.jobs.d49_scan import run_d49_scan

        result = await run_d49_scan()

    assert result["ladders"] == 1
    assert result["signals"] == 2
    assert result["inserted"] == 2
    assert len({s.evidence["pair_key"] for s in inserted}) == 1
    sides = {s.side for s in inserted}
    assert sides == {"YES", "NO"}
    # Нет WS-кэша (fail-closed fallback на db-цену) -> обе ноги не live.
    for s in inserted:
        assert s.evidence["is_live_k1"] is False
        assert s.evidence["is_live_k2"] is False


@pytest.mark.asyncio
async def test_scan_no_violation_yields_zero_signals():
    end_date = datetime.now(UTC) + timedelta(hours=6)
    rows = [
        _row("0x1", "Will BTC be above $60,000 on July 10?", 0.60, end_date),
        _row("0x2", "Will BTC be above $62,000 on July 10?", 0.40, end_date),
    ]
    repo_mock = AsyncMock()
    repo_mock.insert = AsyncMock(return_value=True)

    with (
        patch("app.jobs.d49_scan.fetch", AsyncMock(return_value=rows)),
        patch("app.jobs.d49_scan.get_detector_signal_repository", return_value=repo_mock),
        patch("app.jobs.d49_scan.fetch_live_book", AsyncMock(return_value=None)),
        patch("app.services.live_prices.cache_get", _no_live_cache()),
    ):
        from app.jobs.d49_scan import run_d49_scan

        result = await run_d49_scan()

    assert result["ladders"] == 1
    assert result["signals"] == 0
    assert result["inserted"] == 0
    repo_mock.insert.assert_not_awaited()


@pytest.mark.asyncio
async def test_scan_tx_hash_stable_across_reruns():
    """Дедуп на уровне БД (uq_detector_signals_d49_tx) работает только если tx_hash
    детерминирован между прогонами. Тот же (pair, leg) в том же 4ч-бакете → тот же
    tx_hash → ON CONFLICT DO NOTHING сматчит. Проверяем стабильность ключа: два
    прогона скана в пределах одного бакета дают идентичные tx_hash обеих ног."""
    end_date = datetime.now(UTC) + timedelta(hours=6)
    rows = [
        _row("0x1", "Will BTC be above $60,000 on July 10?", 0.40, end_date),
        _row("0x2", "Will BTC be above $62,000 on July 10?", 0.60, end_date),
    ]

    async def _run_once():
        captured = []
        repo_mock = AsyncMock()
        repo_mock.insert = AsyncMock(side_effect=lambda s: captured.append(s) or True)
        with (
            patch("app.jobs.d49_scan.fetch", AsyncMock(return_value=rows)),
            patch("app.jobs.d49_scan.get_detector_signal_repository", return_value=repo_mock),
            patch("app.jobs.d49_scan.fetch_live_book", AsyncMock(return_value=None)),
            patch("app.services.live_prices.cache_get", _no_live_cache()),
        ):
            from app.jobs.d49_scan import run_d49_scan

            await run_d49_scan()
        return {s.evidence["leg"]: s.evidence["tx_hash"] for s in captured}

    first = await _run_once()
    second = await _run_once()

    assert first and first == second, "tx_hash должен быть стабилен между прогонами для БД-дедупа"


@pytest.mark.asyncio
async def test_scan_missing_clob_tokens_does_not_crash():
    """clob_token_ids=None (кривой ряд) не должен ронять скан — книга и live-цена
    просто пропускаются, сигнал эмитится с db-ценой, token_id=None."""
    end_date = datetime.now(UTC) + timedelta(hours=6)
    rows = [
        {**_row("0x1", "Will BTC be above $60,000 on July 10?", 0.40, end_date), "clob_token_ids": None},
        {**_row("0x2", "Will BTC be above $62,000 on July 10?", 0.60, end_date), "clob_token_ids": None},
    ]
    repo_mock = AsyncMock()
    repo_mock.insert = AsyncMock(return_value=True)

    with (
        patch("app.jobs.d49_scan.fetch", AsyncMock(return_value=rows)),
        patch("app.jobs.d49_scan.get_detector_signal_repository", return_value=repo_mock),
        patch("app.jobs.d49_scan.fetch_live_book", AsyncMock(return_value=None)),
        patch("app.services.live_prices.cache_get", _no_live_cache()),
    ):
        from app.jobs.d49_scan import run_d49_scan

        result = await run_d49_scan()

    assert result["signals"] == 2  # нарушение монотонности → пара, скан не упал


@pytest.mark.asyncio
async def test_scan_touch_market_not_matched_no_signals():
    """'reach $X' рынки не матчатся вообще — 0 лесенок, даже если бы цены нарушали монотонность."""
    end_date = datetime.now(UTC) + timedelta(hours=6)
    rows = [
        _row("0x1", "Will BTC reach $60,000 on July 10?", 0.02, end_date),
        _row("0x2", "Will BTC reach $62,000 on July 10?", 0.30, end_date),
    ]
    repo_mock = AsyncMock()
    repo_mock.insert = AsyncMock(return_value=True)

    with (
        patch("app.jobs.d49_scan.fetch", AsyncMock(return_value=rows)),
        patch("app.jobs.d49_scan.get_detector_signal_repository", return_value=repo_mock),
        patch("app.jobs.d49_scan.fetch_live_book", AsyncMock(return_value=None)),
        patch("app.services.live_prices.cache_get", _no_live_cache()),
    ):
        from app.jobs.d49_scan import run_d49_scan

        result = await run_d49_scan()

    assert result["ladders"] == 0
    assert result["signals"] == 0
    repo_mock.insert.assert_not_awaited()


@pytest.mark.asyncio
async def test_scan_live_price_override_changes_outcome():
    """DB-цена монотонна (нет сигнала), но live WS-кэш даёт нарушение -> сигналы эмитятся."""
    end_date = datetime.now(UTC) + timedelta(hours=6)
    rows = [
        _row("0x1", "Will BTC be above $60,000 on July 10?", 0.60, end_date),  # db: монотонно
        _row("0x2", "Will BTC be above $62,000 on July 10?", 0.40, end_date),
    ]
    inserted = []

    async def _fake_insert(signal):
        inserted.append(signal)
        return True

    repo_mock = AsyncMock()
    repo_mock.insert = AsyncMock(side_effect=_fake_insert)

    async def _fake_cache_get(key):
        # live override: leg K1 (yes_0x1) падает до 0.02, leg K2 (yes_0x2) остаётся 0.40 -> нарушение
        if key == "microstructure:yes_0x1":
            return {"midpoint": 0.02, "epoch": 42.0}
        if key == "ws:conn_epoch":
            return {"epoch": 42.0}
        if key == "ws:last_message_at":
            return {"ts": time.time()}
        return None

    with (
        patch("app.jobs.d49_scan.fetch", AsyncMock(return_value=rows)),
        patch("app.jobs.d49_scan.get_detector_signal_repository", return_value=repo_mock),
        patch("app.jobs.d49_scan.fetch_live_book", AsyncMock(return_value=None)),
        patch("app.services.live_prices.cache_get", AsyncMock(side_effect=_fake_cache_get)),
    ):
        from app.jobs.d49_scan import run_d49_scan

        result = await run_d49_scan()

    assert result["live_priced"] == 1
    assert result["signals"] == 2
    assert result["inserted"] == 2
    # Рассинхрон ног: K1 (0x1, yes_low leg) live-приоритет тикнул, K2 (0x2, no_high leg)
    # остался на db-цене (нет WS-ключа для yes_0x2) — is_live_k1/is_live_k2 в evidence
    # обеих ног должны отражать этот рассинхрон для shadow-аудита (Opus-ревью IMPORTANT).
    for s in inserted:
        assert s.evidence["is_live_k1"] is True
        assert s.evidence["is_live_k2"] is False
