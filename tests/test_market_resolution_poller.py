"""Тесты market_resolution_poller — demand-driven резолв рынков с открытыми позициями.

Root cause (2026-07-07, 6 зависших live-позиций D3): bulk-каналы резолва не
гарантируют покрытие наших позиций — market_sync тянет только 1500 последних
closed events (окно ~2% firehose), а все 3 sweep'а resolve_outcomes требуют
INNER JOIN oracle_snapshots (рынок без whale-снапшотов невидим). Poller
инвертирует направление: перечисляет рынки НАШИХ открытых ордеров и опрашивает
CLOB API точечно — GET /markets/{condition_id} отдаёт явный tokens[].winner.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import httpx
import pytest

from app.jobs import market_resolution_poller as mrp

YES_TOKEN = "111000111"
NO_TOKEN = "222000222"

DEMAND_ROW = {
    "internal_id": "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee",
    "polymarket_id": "0xcond1",
    "condition_id": "0xcond1",
    "yes_token_id": YES_TOKEN,
    "no_token_id": NO_TOKEN,
    "end_date": datetime.now(UTC) - timedelta(days=1),
    "question": "Will the lowest temperature in Shanghai be 22°C on July 2?",
}


def _clob_market(yes_winner: bool | None, no_winner: bool | None) -> dict:
    """CLOB /markets/{condition_id} payload (усечённый до нужных полей)."""
    return {
        "question": DEMAND_ROW["question"],
        "closed": True,
        "tokens": [
            {"token_id": YES_TOKEN, "outcome": "Yes", "price": 0, "winner": yes_winner},
            {"token_id": NO_TOKEN, "outcome": "No", "price": 1, "winner": no_winner},
        ],
    }


# --- _extract_winner: интерпретация CLOB-ответа ---


def test_extract_winner_no_side_wins():
    winner = mrp._extract_winner(_clob_market(False, True), YES_TOKEN, NO_TOKEN)
    assert winner == "no"


def test_extract_winner_yes_side_wins():
    winner = mrp._extract_winner(_clob_market(True, False), YES_TOKEN, NO_TOKEN)
    assert winner == "yes"


def test_extract_winner_unresolved_returns_none():
    """Оба winner=False — рынок закрыт, но UMA ещё не финализировал. Ждём."""
    assert mrp._extract_winner(_clob_market(False, False), YES_TOKEN, NO_TOKEN) is None


def test_extract_winner_two_winners_returns_none():
    """Аномалия API (2 winner'а) — не гадаем с деньгами, пропускаем."""
    assert mrp._extract_winner(_clob_market(True, True), YES_TOKEN, NO_TOKEN) is None


def test_extract_winner_unknown_token_id_returns_none():
    """winner-токен не матчится с нашими clob_token_ids — не гадаем по позиции."""
    market = _clob_market(False, True)
    market["tokens"][1]["token_id"] = "999_unknown"
    assert mrp._extract_winner(market, YES_TOKEN, NO_TOKEN) is None


def test_extract_winner_empty_tokens_returns_none():
    assert mrp._extract_winner({"tokens": []}, YES_TOKEN, NO_TOKEN) is None


# --- run_market_resolution_poll: оркестрация ---


@pytest.mark.asyncio
async def test_poll_empty_demand_set(monkeypatch):
    async def mock_fetch(_sql, *args):
        return []

    monkeypatch.setattr(mrp, "fetch", mock_fetch)

    result = await mrp.run_market_resolution_poll()

    assert result == {"status": "ok", "checked": 0, "resolved": 0, "pending": 0, "errors": 0}


@pytest.mark.asyncio
async def test_poll_resolves_market_and_triggers_snapshots(monkeypatch):
    """CLOB winner=NO → UPDATE markets.resolved/winner + resolve_single_market(price=0.0).

    Дальше _settle_filled() подхватывает сам (гейт m.resolved AND m.winner) —
    здесь проверяем только запись в markets и триггер снапшотов.
    """

    async def mock_fetch(_sql, *args):
        return [dict(DEMAND_ROW)]

    async def mock_fetch_clob(_client, condition_id):
        assert condition_id == "0xcond1"
        return _clob_market(False, True)

    captured_updates: list = []

    async def mock_execute(sql, *args):
        captured_updates.append((sql, args))

    resolve_calls: list = []

    async def mock_resolve_single_market(market_id, outcome, price):
        resolve_calls.append((market_id, outcome, price))
        return 0

    monkeypatch.setattr(mrp, "fetch", mock_fetch)
    monkeypatch.setattr(mrp, "execute", mock_execute)
    monkeypatch.setattr(mrp, "_fetch_clob_market", mock_fetch_clob)
    monkeypatch.setattr("app.tasks.resolve_outcomes.resolve_single_market", mock_resolve_single_market)

    result = await mrp.run_market_resolution_poll()

    assert result == {"status": "ok", "checked": 1, "resolved": 1, "pending": 0, "errors": 0}
    assert len(captured_updates) == 1
    sql, args = captured_updates[0]
    assert "resolved = true" in sql
    assert args == ("no", DEMAND_ROW["internal_id"])
    assert resolve_calls == [(DEMAND_ROW["internal_id"], "no", 0.0)]


@pytest.mark.asyncio
async def test_poll_yes_winner_passes_price_one(monkeypatch):
    async def mock_fetch(_sql, *args):
        return [dict(DEMAND_ROW)]

    async def mock_fetch_clob(_client, _condition_id):
        return _clob_market(True, False)

    async def mock_execute(_sql, *args):
        pass

    resolve_calls: list = []

    async def mock_resolve_single_market(market_id, outcome, price):
        resolve_calls.append((market_id, outcome, price))
        return 0

    monkeypatch.setattr(mrp, "fetch", mock_fetch)
    monkeypatch.setattr(mrp, "execute", mock_execute)
    monkeypatch.setattr(mrp, "_fetch_clob_market", mock_fetch_clob)
    monkeypatch.setattr("app.tasks.resolve_outcomes.resolve_single_market", mock_resolve_single_market)

    result = await mrp.run_market_resolution_poll()

    assert result["resolved"] == 1
    assert resolve_calls == [(DEMAND_ROW["internal_id"], "yes", 1.0)]


@pytest.mark.asyncio
async def test_poll_unresolved_market_stays_pending(monkeypatch):
    """CLOB оба winner=False (настоящая UMA-задержка) → без UPDATE, retry следующим тиком."""

    async def mock_fetch(_sql, *args):
        return [dict(DEMAND_ROW)]

    async def mock_fetch_clob(_client, _condition_id):
        return _clob_market(False, False)

    async def mock_execute(_sql, *args):
        raise AssertionError("UPDATE не должен вызываться для нерезолвнутого рынка")

    monkeypatch.setattr(mrp, "fetch", mock_fetch)
    monkeypatch.setattr(mrp, "execute", mock_execute)
    monkeypatch.setattr(mrp, "_fetch_clob_market", mock_fetch_clob)

    result = await mrp.run_market_resolution_poll()

    assert result == {"status": "ok", "checked": 1, "resolved": 0, "pending": 1, "errors": 0}


@pytest.mark.asyncio
async def test_poll_clob_error_counts_error_and_continues(monkeypatch):
    """HTTP-ошибка на одном рынке не роняет джобу и не блокирует остальные."""

    row2 = {**DEMAND_ROW, "internal_id": "22222222-bbbb-cccc-dddd-eeeeeeeeeeee", "condition_id": "0xcond2"}

    async def mock_fetch(_sql, *args):
        return [dict(DEMAND_ROW), row2]

    async def mock_fetch_clob(_client, condition_id):
        if condition_id == "0xcond1":
            return None  # HTTP failure → None
        return _clob_market(False, True)

    captured_updates: list = []

    async def mock_execute(_sql, *args):
        captured_updates.append(args)

    async def mock_resolve_single_market(_market_id, _outcome, _price):
        return 0

    monkeypatch.setattr(mrp, "fetch", mock_fetch)
    monkeypatch.setattr(mrp, "execute", mock_execute)
    monkeypatch.setattr(mrp, "_fetch_clob_market", mock_fetch_clob)
    monkeypatch.setattr("app.tasks.resolve_outcomes.resolve_single_market", mock_resolve_single_market)

    result = await mrp.run_market_resolution_poll()

    assert result == {"status": "ok", "checked": 2, "resolved": 1, "pending": 0, "errors": 1}
    assert len(captured_updates) == 1
    assert captured_updates[0] == ("no", row2["internal_id"])


@pytest.mark.asyncio
async def test_poll_stuck_market_alerts_admin_once_a_day(monkeypatch):
    """Рынок висит нерезолвнутым >3 дней после end_date → TG-алерт (debounce 24ч)."""

    stuck_row = {**DEMAND_ROW, "end_date": datetime.now(UTC) - timedelta(days=4)}

    async def mock_fetch(_sql, *args):
        return [stuck_row]

    async def mock_fetch_clob(_client, _condition_id):
        return _clob_market(False, False)

    notify_calls: list[list] = []

    async def mock_publish(notifications, actor):
        notify_calls.append(list(notifications))
        return "PublishResult(sent=1)"

    async def mock_debounce_acquire(_key, ttl):
        return True

    monkeypatch.setattr(mrp, "fetch", mock_fetch)
    monkeypatch.setattr(mrp, "_fetch_clob_market", mock_fetch_clob)
    monkeypatch.setattr(mrp, "debounce_acquire", mock_debounce_acquire)
    monkeypatch.setattr(mrp, "publish", mock_publish)

    result = await mrp.run_market_resolution_poll()

    assert result["pending"] == 1
    assert len(notify_calls) == 1
    notes = notify_calls[0]
    assert len(notes) == 1
    assert "Shanghai" in " ".join(notes[0].what_happened)


@pytest.mark.asyncio
async def test_poll_stuck_alert_suppressed_by_debounce(monkeypatch):
    """Второй прогон в те же сутки — алерт не дублируется."""

    stuck_row = {**DEMAND_ROW, "end_date": datetime.now(UTC) - timedelta(days=4)}

    async def mock_fetch(_sql, *args):
        return [stuck_row]

    async def mock_fetch_clob(_client, _condition_id):
        return _clob_market(False, False)

    notify_calls: list[str] = []

    async def mock_send_admin_message(text):
        notify_calls.append(text)

    async def mock_debounce_acquire(_key, ttl):
        return False

    monkeypatch.setattr(mrp, "fetch", mock_fetch)
    monkeypatch.setattr(mrp, "_fetch_clob_market", mock_fetch_clob)
    monkeypatch.setattr(mrp, "debounce_acquire", mock_debounce_acquire)
    monkeypatch.setattr("app.tasks.notifications.send_admin_message", mock_send_admin_message)

    await mrp.run_market_resolution_poll()

    assert notify_calls == []


@pytest.mark.asyncio
async def test_poll_fresh_pending_market_no_alert(monkeypatch):
    """Рынок закрылся вчера и ещё не резолвнут — норма (UMA liveness), алерт не нужен."""

    async def mock_fetch(_sql, *args):
        return [dict(DEMAND_ROW)]  # end_date = -1 день

    async def mock_fetch_clob(_client, _condition_id):
        return _clob_market(False, False)

    notify_calls: list[str] = []

    async def mock_send_admin_message(text):
        notify_calls.append(text)

    monkeypatch.setattr(mrp, "fetch", mock_fetch)
    monkeypatch.setattr(mrp, "_fetch_clob_market", mock_fetch_clob)
    monkeypatch.setattr("app.tasks.notifications.send_admin_message", mock_send_admin_message)

    await mrp.run_market_resolution_poll()

    assert notify_calls == []


# --- _fetch_all_clob_markets: fetch-фаза (переиспользуемый client, bounded concurrency) ---


@pytest.mark.asyncio
async def test_fetch_all_clob_markets_reuses_single_client(monkeypatch):
    """Все fetch'ы фазы идут через ОДИН httpx.AsyncClient — не новый на каждый рынок."""

    row2 = {**DEMAND_ROW, "internal_id": "22222222-bbbb-cccc-dddd-eeeeeeeeeeee", "condition_id": "0xcond2"}
    seen_clients: list[httpx.AsyncClient] = []

    async def mock_fetch_clob(client, condition_id):
        seen_clients.append(client)
        return _clob_market(False, True) if condition_id == "0xcond1" else _clob_market(True, False)

    monkeypatch.setattr(mrp, "_fetch_clob_market", mock_fetch_clob)

    result = await mrp._fetch_all_clob_markets([dict(DEMAND_ROW), row2])

    assert len(seen_clients) == 2
    assert seen_clients[0] is seen_clients[1]
    assert result["0xcond1"]["tokens"][1]["winner"] is True
    assert result["0xcond2"]["tokens"][0]["winner"] is True


@pytest.mark.asyncio
async def test_fetch_all_clob_markets_partial_failure_does_not_affect_others(monkeypatch):
    """Один рынок кидает исключение мимо try/except _fetch_clob_market (defensive
    backstop return_exceptions=True) — второй результат всё равно приходит корректным."""

    row2 = {**DEMAND_ROW, "internal_id": "22222222-bbbb-cccc-dddd-eeeeeeeeeeee", "condition_id": "0xcond2"}

    async def mock_fetch_clob(_client, condition_id):
        if condition_id == "0xcond1":
            raise RuntimeError("boom — мимо внутреннего try/except")
        return _clob_market(False, True)

    monkeypatch.setattr(mrp, "_fetch_clob_market", mock_fetch_clob)

    result = await mrp._fetch_all_clob_markets([dict(DEMAND_ROW), row2])

    assert result["0xcond1"] is None
    assert result["0xcond2"]["tokens"][1]["winner"] is True


# --- SQL-контракт demand set ---


def test_demand_sql_covers_live_and_virtual_open_orders():
    """Demand set = открытые ордера всех стадий: live (placed/filled) + virtual_filled.

    Регрессия-защита: настройка живых денег не должна снова оказаться привязанной
    к oracle_snapshots или к окну пагинации market_sync.
    """
    assert "'placed'" in mrp._DEMAND_SQL
    assert "'filled'" in mrp._DEMAND_SQL
    assert "'virtual_filled'" in mrp._DEMAND_SQL
    assert "oracle_snapshots" not in mrp._DEMAND_SQL
    assert "resolved = false" in mrp._DEMAND_SQL
    assert "condition_id IS NOT NULL" in mrp._DEMAND_SQL
