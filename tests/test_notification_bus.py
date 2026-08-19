from unittest.mock import AsyncMock, patch

import pytest

from app.analytics.notification import ActionSpec, Notification, Severity
from app.services.notification_bus import publish


def _n(severity=Severity.ACTION_REQUIRED, subject="detector:D38_resolution_lag", kind="edge_decay", **over):
    kw = dict(
        severity=severity,
        subject=subject,
        kind=kind,
        headline="Преимущество тает",
        what_happened=["Доходность ушла в минус."],
        what_if_ignored=None,
        what_to_do="Нажми 🔍.",
        actions=[],
        evidence=["run #533"],
    )
    kw.update(over)
    return Notification(**kw)


@pytest.fixture
def bus_mocks():
    with (
        patch("app.services.notification_bus.fetchrow", new=AsyncMock(return_value=None)) as fr,
        patch("app.services.notification_bus.execute", new=AsyncMock()) as ex,
        patch("app.services.notification_bus.send_telegram_with_keyboard_rows", new=AsyncMock(return_value=True)) as tg,
        patch("app.services.notification_bus.get_settings") as gs,
    ):
        gs.return_value.admin_telegram_ids = [111]
        yield {"fetchrow": fr, "execute": ex, "tg": tg}


@pytest.mark.asyncio
async def test_action_required_is_sent(bus_mocks):
    res = await publish([_n()], actor="edge_decay_monitor")
    assert res.sent == 1
    assert bus_mocks["tg"].await_count == 1


@pytest.mark.asyncio
async def test_telemetry_never_reaches_telegram(bus_mocks):
    res = await publish([_n(severity=Severity.TELEMETRY, what_to_do=None)], actor="x")
    assert res.sent == 0
    assert bus_mocks["tg"].await_count == 0


@pytest.mark.asyncio
async def test_fyi_is_digested_not_sent(bus_mocks):
    res = await publish([_n(severity=Severity.FYI, what_to_do=None)], actor="x")
    assert res.sent == 0
    assert res.digested == 1
    assert bus_mocks["tg"].await_count == 0


@pytest.mark.asyncio
async def test_fyi_is_persisted_for_digest(bus_mocks):
    """Риск №1 спеки §10: FYI обязан ДОЙТИ до дайджеста, а не просто не уйти в TG.

    До этого теста publish() на FYI делал digested+=1 и молча выбрасывал
    уведомление — счётчик рос, сообщение исчезало, потребителя не было нигде.
    Проверка `sent == 0` (тест выше) при такой потере остаётся зелёной, поэтому
    факт сохранения проверяется отдельно и по содержимому строки.
    """
    res = await publish([_n(severity=Severity.FYI, what_to_do=None, subject="db:size")], actor="db_size_report")
    assert res.digested == 1

    inserts = [c for c in bus_mocks["execute"].await_args_list if "notification_fyi_queue" in str(c[0][0])]
    assert len(inserts) == 1, "FYI не сохранён в очередь дайджеста — потеря сообщения"
    args = inserts[0][0]
    assert args[1] == "db:size"
    assert args[3] == "db_size_report"
    assert "Преимущество тает" in args[5]


@pytest.mark.asyncio
async def test_fyi_persist_failure_does_not_break_other_notifications(bus_mocks):
    """Сбой записи FYI не должен ронять публикацию ACTION_REQUIRED из той же пачки:
    FYI — справочный поток, ACTION_REQUIRED несёт решение о деньгах."""
    bus_mocks["execute"].side_effect = [RuntimeError("db down"), None, None]

    res = await publish(
        [_n(severity=Severity.FYI, what_to_do=None, subject="db:size"), _n()],
        actor="x",
    )

    assert res.sent == 1
    assert bus_mocks["tg"].await_count == 1


@pytest.mark.asyncio
async def test_telemetry_is_not_persisted_to_fyi_queue(bus_mocks):
    """TELEMETRY не идёт ни в TG, ни в дайджест (спека §4): только БД джобы и дашборд."""
    await publish([_n(severity=Severity.TELEMETRY, what_to_do=None)], actor="x")

    inserts = [c for c in bus_mocks["execute"].await_args_list if "notification_fyi_queue" in str(c[0][0])]
    assert inserts == []


@pytest.mark.asyncio
async def test_duplicate_within_cooldown_is_suppressed(bus_mocks):
    """Time-based cooldown по стабильному ключу (урок PR #741)."""
    bus_mocks["fetchrow"].return_value = {"last_sent_at": "recent"}
    res = await publish([_n()], actor="x")
    assert res.suppressed == 1
    assert bus_mocks["tg"].await_count == 0


@pytest.mark.asyncio
async def test_same_subject_is_merged_into_one_message(bus_mocks):
    """Кейс D38 из спеки §1 п.4: две джобы, одна проблема — одно сообщение."""
    res = await publish([_n(kind="edge_decay"), _n(kind="policy_verdict")], actor="x")
    assert bus_mocks["tg"].await_count == 1
    assert res.sent == 1
    text = bus_mocks["tg"].await_args[0][1]
    assert "Преимущество тает" in text


@pytest.mark.asyncio
async def test_different_subjects_are_not_merged(bus_mocks):
    res = await publish([_n(subject="detector:D38_resolution_lag"), _n(subject="detector:D3_cross_mkt_arb")], actor="x")
    assert bus_mocks["tg"].await_count == 2
    assert res.sent == 2


@pytest.mark.asyncio
async def test_action_creates_agent_actions_row_with_source_actor(bus_mocks):
    """actor обязан быть источника, не 'policy_engine' — иначе журнал врёт."""
    action = ActionSpec(kind="pause_detector", payload={"detector": "D38"}, snapshot={"stage": "shadow"})
    await publish([_n(actions=[action])], actor="edge_decay_monitor")
    insert_calls = [c for c in bus_mocks["execute"].await_args_list if "agent_actions" in str(c[0][0])]
    assert len(insert_calls) == 1
    assert "edge_decay_monitor" in insert_calls[0][0]


@pytest.mark.asyncio
async def test_unknown_action_kind_is_rejected_before_insert(bus_mocks):
    """Риск №3 Phase 3: kind вне whitelist не должен доходить до executor."""
    action = ActionSpec(kind="delete_everything", payload={}, snapshot={})
    res = await publish([_n(actions=[action])], actor="x")
    insert_calls = [c for c in bus_mocks["execute"].await_args_list if "agent_actions" in str(c[0][0])]
    assert insert_calls == []
    assert res.sent == 1  # сообщение уходит, но без кнопки исполнения


@pytest.mark.asyncio
async def test_telegram_failure_is_counted_not_swallowed(bus_mocks):
    bus_mocks["tg"].return_value = False
    res = await publish([_n()], actor="x")
    assert res.failures == 1
    assert res.sent == 0


@pytest.mark.asyncio
async def test_different_kind_same_subject_gets_different_pid(bus_mocks):
    """CRITICAL 1: kind=A и kind=B на тот же actor+subject не должны схлопнуться
    в один idempotency_key — иначе второй INSERT молча топится ON CONFLICT."""
    action_a = ActionSpec(kind="pause_detector", payload={"detector": "D38"}, snapshot={"stage": "shadow"})
    action_b = ActionSpec(kind="reduce_cap", payload={"detector": "D38"}, snapshot={"stage": "shadow"})

    await publish([_n(kind="edge_decay", actions=[action_a])], actor="x")
    pid_day1 = [c for c in bus_mocks["execute"].await_args_list if "agent_actions" in str(c[0][0])][0][0][5]

    bus_mocks["execute"].reset_mock()
    await publish([_n(kind="policy_verdict", actions=[action_b])], actor="x")
    pid_day2 = [c for c in bus_mocks["execute"].await_args_list if "agent_actions" in str(c[0][0])][0][0][5]

    assert pid_day1 != pid_day2
    assert bus_mocks["execute"].await_count >= 1  # второй INSERT реально произошёл


@pytest.mark.asyncio
async def test_same_kind_after_cooldown_window_gets_different_pid(bus_mocks):
    """CRITICAL 1 (второй сценарий): тот же kind+subject+actor, но окно cooldown
    истекло (25ч+) — bucket обязан смениться, иначе кнопка второго сообщения
    указывает на протухшую строку вчерашнего дня."""
    import app.services.notification_bus as bus_mod

    action = ActionSpec(kind="pause_detector", payload={"detector": "D38"}, snapshot={"stage": "shadow"})

    real_time = bus_mod.time.time()
    with patch.object(bus_mod.time, "time", return_value=real_time):
        await publish([_n(actions=[action])], actor="x")
    pid_window1 = [c for c in bus_mocks["execute"].await_args_list if "agent_actions" in str(c[0][0])][0][0][5]

    bus_mocks["execute"].reset_mock()
    with patch.object(bus_mod.time, "time", return_value=real_time + 26 * 3600):
        await publish([_n(actions=[action])], actor="x")
    pid_window2 = [c for c in bus_mocks["execute"].await_args_list if "agent_actions" in str(c[0][0])][0][0][5]

    assert pid_window1 != pid_window2


@pytest.mark.asyncio
async def test_partial_delivery_does_not_write_cooldown(bus_mocks):
    """CRITICAL 2: два админа, первому доставилось, второму нет — cooldown НЕ
    пишется, иначе второй админ не увидит сообщение никогда (подавится на 25ч)."""
    with patch("app.services.notification_bus.get_settings") as gs:
        gs.return_value.admin_telegram_ids = [111, 222]
        bus_mocks["tg"].side_effect = [True, False]
        res = await publish([_n()], actor="x")

    assert res.failures == 1
    mark_sent_calls = [c for c in bus_mocks["execute"].await_args_list if "notification_log" in str(c[0][0])]
    assert mark_sent_calls == []


def test_merge_rejects_mixed_severity():
    """MINOR 4: _merge() — публичная функция, инвариант однородного severity
    должен быть защищён явно, а не молча брать severity первого элемента."""
    from app.services.notification_bus import _merge

    items = [_n(severity=Severity.ACTION_REQUIRED), _n(severity=Severity.FYI, what_to_do=None)]
    with pytest.raises(AssertionError):
        _merge(items)
