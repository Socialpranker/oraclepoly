"""Тесты app.services.basket_execution — multi-leg basket координатор (D29/D40, Task 4).

D29 emits ОДИН сигнал на event, но basket-arb требует buy NO на ВСЕ рынки события
атомарно (иначе exposure 1-leg вместо гарантированного basket payout). place_basket
проверяет депту ВСЕХ ног ДО первого POST, размещает последовательные FOK-ноги и
делает unwind (SELL через close_live_position) при первом non-fill.
"""

from __future__ import annotations

import uuid
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.services.basket_execution import place_basket

BASKET_ID = uuid.UUID("11111111-1111-1111-1111-111111111111")

LEGS = [
    {"token_id": "tok-a", "stake_usd": 10.0, "price_cap": 0.55, "market_id": "mkt-a", "event_id": "evt-1"},
    {"token_id": "tok-b", "stake_usd": 10.0, "price_cap": 0.45, "market_id": "mkt-b", "event_id": "evt-1"},
    {"token_id": "tok-c", "stake_usd": 10.0, "price_cap": 0.35, "market_id": "mkt-c", "event_id": "evt-1"},
]

GOOD_BOOK = {"bid": 0.40, "ask": 0.50, "bid_size": 200.0, "ask_size": 200.0}  # ask_size_usd=100 >= stake*2=20
THIN_BOOK = {"bid": 0.40, "ask": 0.50, "bid_size": 1.0, "ask_size": 1.0}  # ask_size_usd=0.5 < 20


class _FakeClobClient:
    """Минимальный stub с методами, которые basket_execution обращается по имени
    (asyncio.to_thread(client.create_and_post_market_order, ...) требует, чтобы
    сам атрибут существовал ДО вызова to_thread — Python резолвит bound method
    как аргумент-выражение)."""

    def create_and_post_market_order(self, **kwargs):
        raise AssertionError("should be patched via asyncio.to_thread side_effect in test")

    def get_order(self, order_id):
        raise AssertionError("should be patched via asyncio.to_thread side_effect in test")


def _fok_matched_resp(order_id: str = "oid-1", price: float = 0.50):
    return {"success": True, "orderID": order_id}


def _get_order_matched(price: float = 0.50, size: float = 20.0):
    return {"status": "MATCHED", "size_matched": size, "original_size": size, "price": price}


def _get_order_cancelled():
    return {"status": "CANCELLED", "size_matched": 0, "original_size": 20.0}


@pytest.mark.asyncio
async def test_all_legs_depth_ok_and_filled_returns_filled():
    """Все ноги проходят depth-check и FOK-fill → 'filled', 3 INSERT + 3 UPDATE, unwind НЕ вызван."""
    insert_calls: list = []
    update_calls: list = []

    async def mock_fetchrow(_sql, *args):
        insert_calls.append(args)
        return {"id": f"leg-db-{len(insert_calls)}"}

    async def mock_execute(_sql, *args):
        update_calls.append(args)
        return "UPDATE 1"

    with (
        patch("app.services.basket_execution.fetchrow", mock_fetchrow),
        patch("app.services.basket_execution.execute", mock_execute),
        patch("app.services.basket_execution._get_clob_client", AsyncMock(return_value=_FakeClobClient())),
        patch(
            "app.services.execution_safety.fetch_live_book",
            AsyncMock(return_value=GOOD_BOOK),
        ),
        patch(
            "app.services.basket_execution.asyncio.to_thread",
            AsyncMock(side_effect=[_fok_matched_resp(), _get_order_matched()] * 3),
        ),
        patch("app.services.live_exit.close_live_position", AsyncMock()) as unwind_mock,
        patch("app.tasks.notifications.send_admin_message", AsyncMock()),
    ):
        result = await place_basket(LEGS, detector="D29_negrisk_basket_arb", basket_id=BASKET_ID)

    assert result == "filled"
    assert len(insert_calls) == 3
    assert len(update_calls) == 3
    for args in update_calls:
        assert args[1] == "filled"
    unwind_mock.assert_not_awaited()


@pytest.mark.asyncio
async def test_depth_fail_any_leg_rejects_before_any_post():
    """Одна нога с тонкой книгой → rejected_depth, ни один _get_clob_client/POST не вызван."""
    call_count = {"book": 0}

    async def mock_fetch_live_book(token_id):
        call_count["book"] += 1
        # Третья нога (tok-c) — тонкая книга.
        return THIN_BOOK if token_id == "tok-c" else GOOD_BOOK

    with (
        patch("app.services.execution_safety.fetch_live_book", mock_fetch_live_book),
        patch("app.services.basket_execution._get_clob_client", AsyncMock()) as client_factory,
        patch("app.services.basket_execution.fetchrow", AsyncMock()) as insert_mock,
        patch("app.tasks.notifications.send_admin_message", AsyncMock()) as notify_mock,
    ):
        result = await place_basket(LEGS, detector="D29_negrisk_basket_arb", basket_id=BASKET_ID)

    assert result == "rejected_depth"
    # Все 3 ноги должны быть depth-checked (не short-circuit на первой — полная картина в алерте).
    assert call_count["book"] == 3
    client_factory.assert_not_awaited()
    insert_mock.assert_not_awaited()
    notify_mock.assert_awaited_once()


@pytest.mark.asyncio
async def test_second_leg_fok_reject_triggers_unwind_of_first():
    """Нога 1 filled, нога 2 FOK-отказ → unwind ноги 1 через close_live_position, статус 'unwound'."""
    insert_calls: list = []
    update_calls: list = []

    async def mock_fetchrow(_sql, *args):
        insert_calls.append(args)
        return {"id": f"leg-db-{len(insert_calls)}"}

    async def mock_execute(_sql, *args):
        update_calls.append(args)
        return "UPDATE 1"

    # Нога 1: POST success + get_order MATCHED. Нога 2: POST success но get_order CANCELLED
    # (FOK не исполнился) x2 retries. Нога 3 никогда не должна быть тронута (early break).
    to_thread_results = [
        _fok_matched_resp("oid-leg1"),
        _get_order_matched(),
        _fok_matched_resp("oid-leg2"),
        _get_order_cancelled(),
        _get_order_cancelled(),
    ]

    with (
        patch("app.services.basket_execution.fetchrow", mock_fetchrow),
        patch("app.services.basket_execution.execute", mock_execute),
        patch("app.services.basket_execution._get_clob_client", AsyncMock(return_value=_FakeClobClient())),
        patch("app.services.execution_safety.fetch_live_book", AsyncMock(return_value=GOOD_BOOK)),
        patch(
            "app.services.basket_execution.asyncio.to_thread",
            AsyncMock(side_effect=to_thread_results),
        ),
        patch(
            "app.services.basket_execution.asyncio.sleep",
            AsyncMock(),
        ),
        patch("app.services.live_exit.close_live_position", AsyncMock(return_value=(True, "sell-oid-1"))) as unwind_mock,
        patch("app.tasks.notifications.send_admin_message", AsyncMock()) as notify_mock,
    ):
        result = await place_basket(LEGS, detector="D29_negrisk_basket_arb", basket_id=BASKET_ID)

    assert result == "unwound"
    # Только 2 ноги дошли до insert (третья не тронута — early break при первом non-fill).
    assert len(insert_calls) == 2
    # unwind вызван РОВНО для налитых ног (leg 1) — не для отказавшей ноги 2.
    unwind_mock.assert_awaited_once_with("leg-db-1", reason="basket_unwind_partial_fill")
    notify_mock.assert_awaited_once()


@pytest.mark.asyncio
async def test_empty_legs_returns_rejected_depth():
    """Пустой legs list → rejected_depth (defensive, не должно случиться на практике)."""
    result = await place_basket([], detector="D29_negrisk_basket_arb", basket_id=BASKET_ID)
    assert result == "rejected_depth"


@pytest.mark.asyncio
async def test_all_legs_fail_immediately_no_unwind_needed():
    """Самая первая нога FOK-отказ → 0 filled legs → unwound с 0 unwind-вызовами (список пуст),
    но алерт всё равно отправлен (partial fill semantics: 0/N filled тоже требует видимости)."""
    insert_calls: list = []
    update_calls: list = []

    async def mock_fetchrow(_sql, *args):
        insert_calls.append(args)
        return {"id": f"leg-db-{len(insert_calls)}"}

    async def mock_execute(_sql, *args):
        update_calls.append(args)
        return "UPDATE 1"

    with (
        patch("app.services.basket_execution.fetchrow", mock_fetchrow),
        patch("app.services.basket_execution.execute", mock_execute),
        patch("app.services.basket_execution._get_clob_client", AsyncMock(return_value=_FakeClobClient())),
        patch("app.services.execution_safety.fetch_live_book", AsyncMock(return_value=GOOD_BOOK)),
        patch(
            "app.services.basket_execution.asyncio.to_thread",
            AsyncMock(return_value={"success": False, "errorMsg": "not enough balance"}),
        ),
        patch("app.services.live_exit.close_live_position", AsyncMock()) as unwind_mock,
        patch("app.tasks.notifications.send_admin_message", AsyncMock()) as notify_mock,
    ):
        result = await place_basket(LEGS, detector="D29_negrisk_basket_arb", basket_id=BASKET_ID)

    assert result == "unwound"
    assert len(insert_calls) == 1  # только первая нога дошла до insert (early break)
    unwind_mock.assert_not_awaited()  # нечего unwind'ить — 0 filled legs
    notify_mock.assert_awaited_once()


# ── D29 wiring: d29_scan flag-gate + v2_execution basket-signal exclusion ──────


@pytest.mark.asyncio
async def test_d29_scan_disabled_by_default_emits_nothing():
    """d29_basket_enabled=False (default) → run_d29_scan возвращает disabled=True,
    не трогает fetch (сохраняет текущее поведение: D29 не эмитит)."""
    from app.core.config import Settings
    from app.jobs.d29_scan import run_d29_scan

    settings = Settings(
        database_url="postgresql://x:x@localhost/x",
        openai_api_key="sk-x",
        redis_url="redis://localhost",
        telegram_bot_token="1:x",
        admin_telegram_ids=[1],
        admin_api_key="x",
        polymarket_api_key="x",
        d29_basket_enabled=False,
    )

    with (
        patch("app.jobs.d29_scan.get_settings", lambda: settings),
        patch("app.jobs.d29_scan.fetch", AsyncMock()) as fetch_mock,
    ):
        result = await run_d29_scan()

    assert result["disabled"] is True
    assert result["inserted"] == 0
    fetch_mock.assert_not_awaited()


@pytest.mark.asyncio
async def test_d29_scan_enabled_flag_proceeds_to_fetch():
    """d29_basket_enabled=True → guard снят, run_d29_scan идёт дальше (fetch вызван)."""
    from app.core.config import Settings
    from app.jobs.d29_scan import run_d29_scan

    settings = Settings(
        database_url="postgresql://x:x@localhost/x",
        openai_api_key="sk-x",
        redis_url="redis://localhost",
        telegram_bot_token="1:x",
        admin_telegram_ids=[1],
        admin_api_key="x",
        polymarket_api_key="x",
        d29_basket_enabled=True,
    )

    with (
        patch("app.jobs.d29_scan.get_settings", lambda: settings),
        patch("app.jobs.d29_scan.fetch", AsyncMock(return_value=[])) as fetch_mock,
    ):
        result = await run_d29_scan()

    assert "disabled" not in result
    fetch_mock.assert_awaited_once()


def test_is_basket_signal_detects_basket_markets_evidence():
    """_is_basket_signal распознаёт D29-подобный сигнал по evidence['basket_markets']
    (n>=2 ног) — detector-agnostic, тот же путь подойдёт D40."""
    from app.jobs.v2_execution import _is_basket_signal

    basket_sig = {
        "detector": "D29_negrisk_basket_arb",
        "evidence": {
            "basket_markets": [
                {"market_id": "m1", "yes_price": 0.5},
                {"market_id": "m2", "yes_price": 0.5},
                {"market_id": "m3", "yes_price": 0.3},
            ]
        },
    }
    single_sig = {"detector": "D3_cross_mkt_arb", "evidence": {"strategy": "cross_market"}}
    empty_evidence_sig = {"detector": "D3_cross_mkt_arb", "evidence": None}
    one_leg_sig = {"detector": "D29_negrisk_basket_arb", "evidence": {"basket_markets": [{"market_id": "m1"}]}}

    assert _is_basket_signal(basket_sig) is True
    assert _is_basket_signal(single_sig) is False
    assert _is_basket_signal(empty_evidence_sig) is False
    assert _is_basket_signal(one_leg_sig) is False  # n=1 не считается basket (defensive)


def test_is_basket_signal_handles_json_string_evidence():
    """evidence приходит как JSON-строка (как в _notify_live_bet) — тоже парсится."""
    import json

    from app.jobs.v2_execution import _is_basket_signal

    sig = {
        "detector": "D29_negrisk_basket_arb",
        "evidence": json.dumps({"basket_markets": [{"market_id": "m1"}, {"market_id": "m2"}]}),
    }
    assert _is_basket_signal(sig) is True


@pytest.mark.asyncio
async def test_process_signal_routes_basket_signal_to_basket_handler():
    """_process_signal ДОЛЖЕН отдать basket-сигнал в _process_basket_signal, а не
    в обычный single-leg путь (иначе купит только primary market — исходный баг)."""
    from app.jobs.v2_execution import _process_signal

    basket_sig = {
        "detector": "D29_negrisk_basket_arb",
        "detector_signal_id": "sig-basket-1",
        "market_id": "m1",
        "token_id": "primary-no-tok",
        "side": "NO",
        "evidence": {
            "basket_markets": [
                {"market_id": "m1", "yes_price": 0.5, "clob_token_ids": ["y1", "n1"]},
                {"market_id": "m2", "yes_price": 0.5, "clob_token_ids": ["y2", "n2"]},
                {"market_id": "m3", "yes_price": 0.3, "clob_token_ids": ["y3", "n3"]},
            ]
        },
    }

    with patch("app.jobs.v2_execution._process_basket_signal", AsyncMock(return_value="opened")) as basket_mock:
        result = await _process_signal(basket_sig)

    assert result == "opened"
    basket_mock.assert_awaited_once_with(basket_sig)


def _basket_live_state():
    state = MagicMock()
    state.stage = "live"
    state.fractional_kelly_today = 0.10
    state.detector = "D29_negrisk_basket_arb"
    state.portfolio_id = None
    return state


@pytest.mark.asyncio
async def test_live_basket_blocked_by_redis_kill_switch():
    """Redis kill_switch должен блокировать live basket — place_basket не вызывается,
    даже когда env-флаг execution_enabled=True (regression: basket-путь раньше
    проверял только check_kill_switch(), а не полный account-level safety gate —
    Redis kill_switch/daemon-пауза/daily-loss не останавливали basket-ордера)."""
    import app.jobs.v2_execution as v2

    sig = {
        "detector": "D29_negrisk_basket_arb",
        "detector_signal_id": "sig-basket-2",
        "market_id": "m1",
        "side": "NO",
        "entry_price": 0.30,
        "calibrated_p": 0.99,
        "meta_p": 0.99,
        "category": None,
        "fee_schedule": None,
        "evidence": {
            "basket_markets": [
                {"market_id": "m1", "yes_price": 0.5, "clob_token_ids": ["y1", "n1"]},
                {"market_id": "m2", "yes_price": 0.5, "clob_token_ids": ["y2", "n2"]},
            ]
        },
    }

    bankroll_snap = MagicMock(bankroll_usd=1000.0, is_sufficient=True)

    with (
        patch.object(v2, "get_detector_state", new=AsyncMock(return_value=_basket_live_state())),
        patch.object(v2, "get_live_bankroll", new=AsyncMock(return_value=bankroll_snap)),
        patch.object(v2, "get_settings") as mock_settings,
        patch.object(v2, "check_kill_switch", return_value=(True, "")),
        patch.object(v2, "check_redis_kill_flag", new=AsyncMock(return_value=(False, "kill_switch:redis_flag_set"))),
        patch("app.services.basket_execution.place_basket", new=AsyncMock()) as basket_mock,
    ):
        mock_settings.return_value.execution_enabled = True
        mock_settings.return_value.d29_basket_enabled = True
        mock_settings.return_value.live_min_stake = 1.0

        result = await v2._process_basket_signal(sig)

    assert result == "rejected"
    basket_mock.assert_not_awaited()
