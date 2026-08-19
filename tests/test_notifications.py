"""Tests для app.tasks.notifications — чанкинг длинных TG-сообщений."""

from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest

from app.tasks.notifications import _split_message, send_admin_message


def test_split_message_short_text_single_part():
    """Текст короче лимита возвращается как один элемент — без изменений."""
    assert _split_message("short text", limit=100) == ["short text"]


def test_split_message_splits_on_line_boundaries():
    """Длинный текст режется по '\\n', не разрывая строку посередине."""
    text = "\n".join(f"line {i}" for i in range(20))
    parts = _split_message(text, limit=30)
    assert len(parts) > 1
    for part in parts:
        assert len(part) <= 30
    # Ни одна строка не порезана — каждая исходная строка целиком внутри одной части
    for i in range(20):
        assert any(f"line {i}" in part for part in parts)


def test_split_message_does_not_exceed_limit():
    """Ни одна часть не превышает limit, даже при длинных строках."""
    text = "x" * 50 + "\n" + "y" * 50 + "\n" + "z" * 50
    parts = _split_message(text, limit=40)
    assert all(len(p) <= 40 for p in parts)


def test_split_message_reassembles_to_original_content():
    """Разбитые части при склейке через '\\n' восстанавливают исходный текст
    (в предположении, что ни одна строка не длиннее limit — крайний случай
    жёсткого реза строки покрыт отдельно)."""
    text = "\n".join(f"строка {i} с русским текстом" for i in range(30))
    parts = _split_message(text, limit=60)
    assert "\n".join(parts) == text


def test_split_message_keeps_section_header_with_its_list():
    """Заголовок раздела (напр. '<b>superseded (5)</b>') не должен остаться
    один в конце чанка, оторванный от списка, который идёт сразу под ним
    через пустую строку — реальный баг, найденный на owner's report."""
    # Заполнитель, чтобы первый флаш случился ровно на границе "хвост
    # заполнителя / заголовок следующей секции" — воспроизводит боевой кейс.
    filler = "\n".join(f"- пункт списка номер {i} с некоторым текстом" for i in range(20))
    header = "<b>superseded (5)</b>"
    section_items = "\n".join(f"- superseded-клейм номер {i}" for i in range(5))
    text = f"{filler}\n\n{header}\n\n{section_items}"

    parts = _split_message(text, limit=len(filler) + 10)

    assert len(parts) > 1
    # Заголовок и первый пункт его списка обязаны попасть в одну часть.
    header_part = next(p for p in parts if header in p)
    assert section_items.split("\n")[0] in header_part


def test_split_message_does_not_cut_right_after_header_before_its_list():
    """Симметричный случай к keeps_section_header_with_its_list: заголовок не
    должен остаться ОДИН в конце части, даже когда пустая строка сразу под
    ним технически 'помещается' — нужно от неё смотреть дальше вперёд, а не
    резать по первой попавшейся пустой строке после заголовка."""
    filler = "\n".join(f"- пункт заполнителя номер {i}" for i in range(15))
    header = "<b>active (49)</b>"
    first_item = "- первый пункт списка active"
    rest_items = "\n".join(f"- пункт active номер {i}" for i in range(2, 10))
    text = f"{filler}\n\n{header}\n\n{first_item}\n{rest_items}"

    # limit подобран так, чтобы "filler + \n\n + header" ровно помещался,
    # а следующая строка (пустая перед first_item) — уже нет.
    limit = len(f"{filler}\n\n{header}") + 2

    parts = _split_message(text, limit=limit)

    header_part = next(p for p in parts if header in p)
    assert first_item in header_part, (
        "заголовок active(49) не должен остаться один в части без первого пункта своего списка"
    )


def test_split_message_falls_back_to_line_boundary_without_blank_lines():
    """Если в накопленном чанке нет ни одной пустой строки (единый параграф
    без разделителей длиннее limit) — деградирует к резу по границе строк,
    как раньше, а не роняет всё в один гигантский пересиженный чанк."""
    text = "\n".join(f"line {i}" for i in range(50))  # без пустых строк
    parts = _split_message(text, limit=40)
    assert all(len(p) <= 40 for p in parts)
    assert "\n".join(parts) == text


@pytest.mark.asyncio
async def test_send_admin_message_short_text_sends_once_per_admin():
    """Короткий текст — ровно один send_telegram_message на админа (старое поведение)."""
    with (
        patch("app.core.config.get_settings") as m_settings,
        patch("app.api.telegram.send_telegram_message", new=AsyncMock()) as m_send,
    ):
        m_settings.return_value.admin_telegram_ids = [111, 222]
        await send_admin_message("short text")

    assert m_send.await_count == 2
    m_send.assert_any_await(111, "short text")
    m_send.assert_any_await(222, "short text")


@pytest.mark.asyncio
async def test_send_admin_message_long_text_splits_into_multiple_sends():
    """Текст длиннее лимита Telegram шлётся несколькими последовательными сообщениями."""
    long_text = "\n".join(f"line {i}: {'x' * 50}" for i in range(200))
    assert len(long_text) > 4000

    with (
        patch("app.core.config.get_settings") as m_settings,
        patch("app.api.telegram.send_telegram_message", new=AsyncMock()) as m_send,
    ):
        m_settings.return_value.admin_telegram_ids = [111]
        await send_admin_message(long_text)

    assert m_send.await_count > 1
    for call in m_send.await_args_list:
        chat_id, part = call.args
        assert chat_id == 111
        assert len(part) <= 4000
