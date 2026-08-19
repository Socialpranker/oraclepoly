from app.analytics.advisory_buttons import (
    ACK,
    INVESTIGATE,
    REVIEW,
    build_advisory_keyboard,
    parse_advisory_callback,
)


def test_keyboard_has_three_buttons_in_one_row():
    rows = build_advisory_keyboard("abc123def4567890")
    assert len(rows) == 1
    assert len(rows[0]) == 3
    actions = [btn["callback_data"].split(":")[1] for btn in rows[0]]
    assert actions == [ACK, INVESTIGATE, REVIEW]


def test_callback_data_within_telegram_limit():
    pid = "abc123def4567890"
    rows = build_advisory_keyboard(pid)
    for btn in rows[0]:
        assert len(btn["callback_data"].encode("utf-8")) <= 64

    # Явная проверка худшего случая: самый длинный action (INVESTIGATE) с pid из 16
    # hex-символов. "adv:" + "investigate" + ":" + 16 символов = 4 + 11 + 1 + 16 = 32 байта.
    # Фиксируем точную длину и остаток запаса (64 - 32 = 32 байта), чтобы будущее
    # удлинение префикса/action/pid молча не проскочило мимо теста.
    investigate_btn = rows[0][1]
    callback_data = investigate_btn["callback_data"]
    assert callback_data == f"adv:investigate:{pid}"
    callback_bytes = len(callback_data.encode("utf-8"))
    assert callback_bytes == 32
    assert 64 - callback_bytes == 32


def test_parse_roundtrip():
    rows = build_advisory_keyboard("abc123def4567890")
    action, pid = parse_advisory_callback(rows[0][1]["callback_data"])
    assert action == INVESTIGATE
    assert pid == "abc123def4567890"


def test_parse_bad_format_returns_none():
    assert parse_advisory_callback("adv:ack") is None
    assert parse_advisory_callback("garbage") is None
    assert parse_advisory_callback("adv:unknown:abc") is None
    assert parse_advisory_callback("adv:ack:xxx:extra") is None
    assert parse_advisory_callback("adv:ack:") is None
