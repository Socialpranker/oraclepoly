"""Тесты для app/jobs/d45_chainlink_leadlag_scan.py (Stage 2 cron).

Покрытие:
- Пустая выборка markets → empty stats.
- Корректный slug + свежий tick + history → Signal вставлен.
- Stale Pyth tick → skipped_stale_tick (нет insert).
- Нет open_price из history → skipped_no_open_price.
- Slug непарсится → skipped_no_symbol.
- Detection ниже threshold → skipped_no_detection.
- Idempotency: tx_hash детерминирован per (market, window_end).
- current_price parser: str / float / Decimal / extreme values.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest

from app.jobs import d45_chainlink_leadlag_scan as scan_mod
from app.services.pyth_stream_service import PythTick


@pytest.fixture(autouse=True)
def _patch_binance_singleton():
    """Все scan-тесты без явного binance параметра должны видеть пустой
    Binance, а не дёргать настоящий get_binance_stream() (он бы попытался
    создать WS connection в test process)."""

    class _NullBinance:
        def get_latest(self, _symbol):  # noqa: ARG002
            return None

    with patch(
        "app.jobs.d45_chainlink_leadlag_scan.get_binance_stream",
        return_value=_NullBinance(),
    ):
        yield

_SENTINEL = object()


def _market_row(
    market_id: str = "m-btc-1",
    slug: str = "btc-updown-5m-1779354900",
    yes_price_str: str = "0.50",
    end_date_ts: int = 1779355200 - 10,
    event_id: str | None = "evt-1",
    clob_token_ids: list[str] | None = None,
    current_price=_SENTINEL,
):
    """Helper: одна строка из active_updown SQL.

    current_price: sentinel значит "default = yes_price_str". Передать явно
    None / "garbage" для специфичных кейсов парсера.
    """
    if current_price is _SENTINEL:
        current_price = yes_price_str
    return {
        "market_id": market_id,
        "condition_id": f"cond-{market_id}",
        "slug": slug,
        "event_id": event_id,
        "clob_token_ids": clob_token_ids or ["tok-yes", "tok-no"],
        "current_price": current_price,
        "end_date_ts": end_date_ts,
    }


def _stream_mock(symbol: str = "BTC/USD", price: float = 78000.0, age_ms: int = 100):
    """Mock PythStreamService.get_latest() возвращает fresh tick для любого
    из 4 поддерживаемых символов (один и тот же price). Это позволяет
    тестам с несколькими markets разных coins использовать один mock."""
    mock = AsyncMock()
    now_ms = 1779354900_000  # within window 1779354600 + 300 = 1779354900
    supported = {"BTC/USD", "ETH/USD", "SOL/USD", "XRP/USD"}
    mock.get_latest = lambda s: (
        PythTick(
            symbol=s,
            price=price,
            publish_time_ms=now_ms - 200,
            received_at_ms=now_ms - age_ms,
        )
        if s in supported
        else None
    )
    # symbol kwarg сохранён для обратной совместимости — больше не используется
    _ = symbol
    return mock


def _stale_stream_mock(symbol: str = "BTC/USD"):
    """Stream вернёт None (имитация stale tick)."""
    mock = AsyncMock()
    mock.get_latest = lambda s: None
    return mock


# ---------- _coerce_yes_midpoint ----------


def test_coerce_yes_midpoint_from_string():
    assert scan_mod._coerce_yes_midpoint("0.62") == pytest.approx(0.62)


def test_coerce_yes_midpoint_from_float():
    assert scan_mod._coerce_yes_midpoint(0.71) == pytest.approx(0.71)


def test_coerce_yes_midpoint_from_decimal():
    from decimal import Decimal

    assert scan_mod._coerce_yes_midpoint(Decimal("0.45")) == pytest.approx(0.45)


def test_coerce_yes_midpoint_none_on_invalid():
    assert scan_mod._coerce_yes_midpoint(None) is None
    assert scan_mod._coerce_yes_midpoint("not-a-number") is None


def test_coerce_yes_midpoint_clamps_extreme_values():
    """0.0 / 1.0 / out-of-range → None (рынок зарешён или сломан)."""
    assert scan_mod._coerce_yes_midpoint(0.0) is None
    assert scan_mod._coerce_yes_midpoint(1.0) is None
    assert scan_mod._coerce_yes_midpoint(-0.1) is None
    assert scan_mod._coerce_yes_midpoint(1.5) is None


# ---------- _market_window_tx_hash ----------


def test_tx_hash_deterministic_per_window():
    h1 = scan_mod._market_window_tx_hash("m-btc-1", 1779355200)
    h2 = scan_mod._market_window_tx_hash("m-btc-1", 1779355200)
    assert h1 == h2
    assert h1.startswith("0x")
    assert len(h1) == 2 + 62


def test_tx_hash_differs_per_market():
    h1 = scan_mod._market_window_tx_hash("m-a", 1779355200)
    h2 = scan_mod._market_window_tx_hash("m-b", 1779355200)
    assert h1 != h2


def test_tx_hash_differs_per_window_end():
    h1 = scan_mod._market_window_tx_hash("m-a", 1779355200)
    h2 = scan_mod._market_window_tx_hash("m-a", 1779355500)
    assert h1 != h2


# ---------- _build_market_state ----------


def test_build_market_state_valid_btc():
    row = _market_row(slug="btc-updown-5m-1779354600", yes_price_str="0.60")
    state = scan_mod._build_market_state(row)
    assert state is not None
    assert state.slug == "btc-updown-5m-1779354600"
    assert state.window_start_ts == 1779354600
    assert state.window_end_ts == 1779354900  # +300
    assert state.horizon_sec == 300
    assert state.yes_midpoint == pytest.approx(0.60)
    assert state.token_id == "tok-yes"


def test_build_market_state_carries_raw_clob_token_ids():
    """raw clob_token_ids пробрасывается в MarketState → to_signal резолвит по side.

    Регрессия: раньше scan хардкодил side_hint='YES' и не сохранял raw ids,
    поэтому NO-сигналы несли YES-токен (инверсия, как баг D3)."""
    row = _market_row(slug="btc-updown-5m-1779354600", clob_token_ids=["yid", "nid"])
    state = scan_mod._build_market_state(row)
    assert state is not None
    assert state.clob_token_ids == ["yid", "nid"]


def test_build_market_state_unparseable_slug():
    row = _market_row(slug="not-an-updown-slug")
    assert scan_mod._build_market_state(row) is None


def test_build_market_state_missing_current_price():
    row = _market_row(current_price=None)
    assert scan_mod._build_market_state(row) is None


def test_build_market_state_rejects_non_chainlink_horizon():
    """Monthly/weekly markets резолвятся Binance, не Chainlink — отсекаются."""
    # 1d = 86400 sec, не в whitelist {300, 900, 14400}
    row = _market_row(slug="btc-updown-1d-1779354600")
    assert scan_mod._build_market_state(row) is None


def test_build_market_state_accepts_15m_horizon():
    """15-minute markets валидны для D45."""
    row = _market_row(slug="eth-updown-15m-1779354600")
    state = scan_mod._build_market_state(row)
    assert state is not None
    assert state.horizon_sec == 900


def test_build_market_state_accepts_4h_horizon():
    """4-hour markets валидны для D45."""
    row = _market_row(slug="sol-updown-4h-1779354600")
    state = scan_mod._build_market_state(row)
    assert state is not None
    assert state.horizon_sec == 14400


# ---------- run_d45_chainlink_leadlag_scan ----------


@pytest.mark.asyncio
async def test_scan_empty_markets():
    with patch("app.jobs.d45_chainlink_leadlag_scan.fetch", AsyncMock(return_value=[])):
        result = await scan_mod.run_d45_chainlink_leadlag_scan(
            stream=_stream_mock(),
            repo=AsyncMock(),
            now_ts=1779354890,
        )
    assert result["scanned"] == 0
    assert result["signals"] == 0


@pytest.mark.asyncio
async def test_scan_emits_signal_when_pyth_diverges():
    """Pyth: BTC поднялся с 77000 (open) до 78540 (+2.0%). CLOB ещё стоит
    на 0.50 (нейтрал). Divergence = +2.0% → YES signal."""
    rows = [_market_row(slug="btc-updown-5m-1779354600", yes_price_str="0.50")]
    repo = AsyncMock()
    repo.insert = AsyncMock(return_value=True)

    with (
        patch("app.jobs.d45_chainlink_leadlag_scan.fetch", AsyncMock(return_value=rows)),
        patch(
            "app.jobs.d45_chainlink_leadlag_scan.get_price_at",
            AsyncMock(return_value=77000.0),
        ),
    ):
        # now_ts = window_end - 10  (= 1779354900 - 10 = 1779354890)
        result = await scan_mod.run_d45_chainlink_leadlag_scan(
            stream=_stream_mock(price=78540.0),
            repo=repo,
            now_ts=1779354890,
        )

    assert result["scanned"] == 1
    assert result["signals"] == 1
    assert result["inserted"] == 1
    repo.insert.assert_awaited_once()
    signal = repo.insert.call_args[0][0]
    assert signal.detector == "d45_chainlink_leadlag"
    assert signal.side == "YES"
    assert "tx_hash" in signal.evidence
    assert signal.evidence["tx_hash"].startswith("0x")


@pytest.mark.asyncio
async def test_scan_no_signal_carries_no_token_not_yes():
    """Pyth упал (−2%), CLOB на 0.50 → side=NO → token_id = NO-токен (index 1).

    Регрессия инверсии (как D3): до фикса все NO-сигналы несли YES-токен →
    в live бот купил бы YES при намерении NO (ставка против сигнала)."""
    rows = [
        _market_row(
            slug="btc-updown-5m-1779354600",
            yes_price_str="0.50",
            clob_token_ids=["tok-yes", "tok-no"],
        )
    ]
    repo = AsyncMock()
    repo.insert = AsyncMock(return_value=True)

    with (
        patch("app.jobs.d45_chainlink_leadlag_scan.fetch", AsyncMock(return_value=rows)),
        patch(
            "app.jobs.d45_chainlink_leadlag_scan.get_price_at",
            AsyncMock(return_value=77000.0),
        ),
    ):
        result = await scan_mod.run_d45_chainlink_leadlag_scan(
            stream=_stream_mock(price=75460.0),  # −2.0% → divergence −2% → NO
            repo=repo,
            now_ts=1779354890,
        )

    assert result["inserted"] == 1
    signal = repo.insert.call_args[0][0]
    assert signal.side == "NO"
    assert signal.token_id == "tok-no"


@pytest.mark.asyncio
async def test_scan_skips_when_no_live_tick():
    rows = [_market_row()]
    repo = AsyncMock()

    with (
        patch("app.jobs.d45_chainlink_leadlag_scan.fetch", AsyncMock(return_value=rows)),
        patch(
            "app.jobs.d45_chainlink_leadlag_scan.get_price_at",
            AsyncMock(return_value=77000.0),
        ),
    ):
        result = await scan_mod.run_d45_chainlink_leadlag_scan(
            stream=_stale_stream_mock(),
            repo=repo,
            now_ts=1779354890,
        )

    assert result["scanned"] == 1
    assert result["skipped_no_live_tick"] == 1
    assert result["signals"] == 0
    repo.insert.assert_not_awaited()


@pytest.mark.asyncio
async def test_scan_skips_when_no_open_price():
    rows = [_market_row()]
    repo = AsyncMock()

    with (
        patch("app.jobs.d45_chainlink_leadlag_scan.fetch", AsyncMock(return_value=rows)),
        patch(
            "app.jobs.d45_chainlink_leadlag_scan.get_price_at",
            AsyncMock(return_value=None),
        ),
    ):
        result = await scan_mod.run_d45_chainlink_leadlag_scan(
            stream=_stream_mock(),
            repo=repo,
            now_ts=1779354890,
        )

    assert result["skipped_no_open_price"] == 1
    assert result["signals"] == 0


@pytest.mark.asyncio
async def test_scan_skips_below_threshold():
    """Pyth move 0.1%, CLOB also 0%. Divergence 0.1% < default 0.3% → skipped."""
    rows = [_market_row(yes_price_str="0.50")]
    repo = AsyncMock()

    with (
        patch("app.jobs.d45_chainlink_leadlag_scan.fetch", AsyncMock(return_value=rows)),
        patch(
            "app.jobs.d45_chainlink_leadlag_scan.get_price_at",
            AsyncMock(return_value=77000.0),
        ),
    ):
        # Pyth poshly only +0.1% → 77077
        result = await scan_mod.run_d45_chainlink_leadlag_scan(
            stream=_stream_mock(price=77077.0),
            repo=repo,
            now_ts=1779354890,
        )

    assert result["skipped_no_detection"] == 1
    assert result["signals"] == 0


@pytest.mark.asyncio
async def test_scan_skips_unknown_slug_format():
    rows = [_market_row(slug="weird-market-without-updown")]
    repo = AsyncMock()

    with patch("app.jobs.d45_chainlink_leadlag_scan.fetch", AsyncMock(return_value=rows)):
        result = await scan_mod.run_d45_chainlink_leadlag_scan(
            stream=_stream_mock(),
            repo=repo,
            now_ts=1779354890,
        )

    assert result["skipped_no_symbol"] == 1
    assert result["signals"] == 0


@pytest.mark.asyncio
async def test_scan_idempotent_window_emits_once():
    """Два scan'а в одном окне резолюции → второй ON CONFLICT DO NOTHING."""
    rows = [_market_row(slug="btc-updown-5m-1779354600", yes_price_str="0.50")]
    repo = AsyncMock()
    repo.insert = AsyncMock(side_effect=[True, False])  # 2-й раз — дубль

    with (
        patch("app.jobs.d45_chainlink_leadlag_scan.fetch", AsyncMock(return_value=rows)),
        patch(
            "app.jobs.d45_chainlink_leadlag_scan.get_price_at",
            AsyncMock(return_value=77000.0),
        ),
    ):
        r1 = await scan_mod.run_d45_chainlink_leadlag_scan(
            stream=_stream_mock(price=78540.0),
            repo=repo,
            now_ts=1779354890,
        )
        # повторный scan через 5s в том же окне
        r2 = await scan_mod.run_d45_chainlink_leadlag_scan(
            stream=_stream_mock(price=78540.0),
            repo=repo,
            now_ts=1779354895,
        )

    # Оба эмитят Signal с одинаковым tx_hash, но второй insert=False (conflict)
    assert r1["inserted"] == 1
    assert r2["inserted"] == 0
    assert r2["signals"] == 1  # detected, но не вставлен

    tx_hash_1 = repo.insert.call_args_list[0][0][0].evidence["tx_hash"]
    tx_hash_2 = repo.insert.call_args_list[1][0][0].evidence["tx_hash"]
    assert tx_hash_1 == tx_hash_2


@pytest.mark.asyncio
async def test_scan_insert_exception_does_not_break_batch():
    """Один insert упал → стат корректен, второй market не пострадал."""
    rows = [
        _market_row(market_id="m1", slug="btc-updown-5m-1779354600"),
        _market_row(market_id="m2", slug="eth-updown-5m-1779354600", clob_token_ids=["e-y", "e-n"]),
    ]
    repo = AsyncMock()
    repo.insert = AsyncMock(side_effect=[Exception("DB write failed"), True])

    with (
        patch("app.jobs.d45_chainlink_leadlag_scan.fetch", AsyncMock(return_value=rows)),
        patch(
            "app.jobs.d45_chainlink_leadlag_scan.get_price_at",
            AsyncMock(return_value=77000.0),
        ),
    ):
        result = await scan_mod.run_d45_chainlink_leadlag_scan(
            stream=_stream_mock(price=78540.0),
            repo=repo,
            now_ts=1779354890,
        )

    assert result["scanned"] == 2
    assert result["signals"] == 2
    assert result["inserted"] == 1  # второй вставлен, первый поглотил exception


def _binance_mock(price: float = 77490.0):
    """Mock BinanceStreamService.get_latest() — fresh tick для всех символов."""
    from app.services.binance_stream_service import BinanceTick

    mock = AsyncMock()
    now_ms = 1779354900_000
    mock.get_latest = lambda s: BinanceTick(
        symbol=s, price=price, trade_time_ms=now_ms - 50, received_at_ms=now_ms - 50
    )
    return mock


def _no_binance_mock():
    """Mock возвращает None — нет Binance ticks."""
    mock = AsyncMock()
    mock.get_latest = lambda s: None
    return mock


@pytest.mark.asyncio
async def test_scan_uses_binance_cross_check_when_agrees():
    """Binance подтверждает Pyth → signal эмитится."""
    rows = [_market_row(slug="btc-updown-5m-1779354600", yes_price_str="0.50")]
    repo = AsyncMock()
    repo.insert = AsyncMock(return_value=True)

    with (
        patch("app.jobs.d45_chainlink_leadlag_scan.fetch", AsyncMock(return_value=rows)),
        patch(
            "app.jobs.d45_chainlink_leadlag_scan.get_price_at",
            AsyncMock(return_value=77000.0),
        ),
    ):
        result = await scan_mod.run_d45_chainlink_leadlag_scan(
            stream=_stream_mock(price=78540.0),
            binance=_binance_mock(price=78500.0),  # basis ~0.05% < 0.5%
            repo=repo,
            now_ts=1779354890,
        )

    assert result["signals"] == 1
    assert result["inserted"] == 1


@pytest.mark.asyncio
async def test_scan_blocks_signal_when_binance_disagrees():
    """Binance отличается на >0.5% → Pyth glitch suspect, signal skipped."""
    rows = [_market_row(slug="btc-updown-5m-1779354600", yes_price_str="0.50")]
    repo = AsyncMock()

    with (
        patch("app.jobs.d45_chainlink_leadlag_scan.fetch", AsyncMock(return_value=rows)),
        patch(
            "app.jobs.d45_chainlink_leadlag_scan.get_price_at",
            AsyncMock(return_value=77000.0),
        ),
    ):
        result = await scan_mod.run_d45_chainlink_leadlag_scan(
            stream=_stream_mock(price=78540.0),
            binance=_binance_mock(price=70000.0),  # basis ~12% >> 0.5%
            repo=repo,
            now_ts=1779354890,
        )

    assert result["skipped_no_detection"] == 1
    assert result["signals"] == 0


@pytest.mark.asyncio
async def test_scan_works_without_binance_ticks():
    """Binance stream connected но нет tick'ов → cross-check выключен, signal по Pyth."""
    rows = [_market_row(slug="btc-updown-5m-1779354600", yes_price_str="0.50")]
    repo = AsyncMock()
    repo.insert = AsyncMock(return_value=True)

    with (
        patch("app.jobs.d45_chainlink_leadlag_scan.fetch", AsyncMock(return_value=rows)),
        patch(
            "app.jobs.d45_chainlink_leadlag_scan.get_price_at",
            AsyncMock(return_value=77000.0),
        ),
    ):
        result = await scan_mod.run_d45_chainlink_leadlag_scan(
            stream=_stream_mock(price=78540.0),
            binance=_no_binance_mock(),
            repo=repo,
            now_ts=1779354890,
        )

    assert result["signals"] == 1
    assert result["inserted"] == 1


@pytest.mark.asyncio
async def test_scan_returns_full_stats_keys():
    """Возвращаемый dict всегда содержит все 8 ключей stats."""
    rows = [_market_row()]
    expected_keys = {
        "scanned",
        "signals",
        "inserted",
        "skipped_no_symbol",
        "skipped_no_live_tick",
        "skipped_stale_tick",
        "skipped_no_open_price",
        "skipped_no_detection",
    }

    with (
        patch("app.jobs.d45_chainlink_leadlag_scan.fetch", AsyncMock(return_value=rows)),
        patch(
            "app.jobs.d45_chainlink_leadlag_scan.get_price_at",
            AsyncMock(return_value=77000.0),
        ),
    ):
        result = await scan_mod.run_d45_chainlink_leadlag_scan(
            stream=_stream_mock(),
            repo=AsyncMock(),
            now_ts=1779354890,
        )

    assert set(result.keys()) == expected_keys


# ── market_id конвенция ──


def test_active_updown_sql_selects_polymarket_id_as_market_id():
    """detector_signals.market_id конвенция = markets.polymarket_id (0x-condition_id).

    Регрессия 2026-07-09: SQL отдавал внутренний UUID `m.id AS market_id` →
    LEFT JOIN в detector_signals_resolve (mo.market_id = ds.market_id, где
    mo.market_id = m.polymarket_id) никогда не матчился → 100% сигналов
    censored (EXPIRED, ноль WIN/LOSS). Эталон конвенции: d38_scan.py.
    """
    normalized = " ".join(scan_mod._ACTIVE_UPDOWN_SQL.split())
    assert "m.polymarket_id AS market_id" in normalized
    assert "m.id AS market_id" not in normalized


def test_active_updown_sql_selects_event_polymarket_id_as_event_id():
    """detector_signals.event_id конвенция = events.polymarket_id (эталон d38_scan.py).

    Регрессия 2026-07-10 (сиквел market_id-бага): SQL отдавал внутренний UUID
    `m.event_id` (FK на events.id) → event-кластеризация аналитики
    (money-funnel, category_performance views группируют по event_id) не
    группировала строки D45. NULL допустим — event_id nullable.
    """
    normalized = " ".join(scan_mod._ACTIVE_UPDOWN_SQL.split())
    assert "e.polymarket_id AS event_id" in normalized
    assert "LEFT JOIN events e ON e.id = m.event_id" in normalized
    assert "m.event_id AS event_id" not in normalized
