"""Admin Notifications — отправка сообщений Telegram-админам.

Используется trading-jobs (execution_reconcile, auto_redeem, retail_trades_sync)
и oracle_worker для критичных алертов про систему.

История: раньше здесь был run_notification_dispatch для рассылки пользователям.
Удалён при переходе на self-trading bot (2026-04-30).
"""

import structlog

logger = structlog.get_logger()

# Telegram Bot API: sendMessage.text лимитирован 4096 UTF-16 code units.
# Небольшой запас на случай многобайтовых символов при округлении.
_TELEGRAM_MESSAGE_LIMIT = 4000


def _is_bare_header_line(line: str) -> bool:
    """Похожа ли строка на markdown/HTML-заголовок раздела, а не на пункт списка.

    Эвристика: непустая строка без ведущего маркера списка ('- '). Заголовки
    отчётов рендерятся как '<b>...</b>' (после _markdown_to_telegram_html)
    или как '## ...' (сырой markdown), пункты списка — как '- ...'.
    """
    return bool(line) and not line.lstrip().startswith("-")


def _split_message(text: str, limit: int = _TELEGRAM_MESSAGE_LIMIT) -> list[str]:
    """Разбить текст на части ≤limit символов по границам абзацев (пустых строк).

    Режет по '\\n', не разрывая строку посередине — иначе можно разорвать
    HTML-тег (<b>/<code>), который send_telegram_message шлёт с parse_mode=HTML.

    Предпочитает резать на пустой строке, непосредственно ПЕРЕД которой не
    стоит голый заголовок раздела (см. _is_bare_header_line) — иначе в
    markdown-отчётах вида '<b>Заголовок</b>\\n\\n- пункт списка' резалось бы
    либо сразу после заголовка (оставляя его в одном сообщении без единого
    пункта своего списка), либо на более ранней "случайно подходящей" пустой
    строке, из-за которой заголовок остаётся в хвосте чанка тоже без пунктов.
    Оба случая проверялись и ловились по отдельности при разработке — простой
    подсчёт числа хвостовых строк недостаточен, т.к. длина реальных пунктов
    списка непредсказуема; только явная проверка "не голый ли заголовок перед
    границей" даёт правильную границу независимо от длины контента.

    Если во всём накопленном чанке нет такой границы — режет там, где строка
    перестаёт помещаться (старое построчное поведение). Одиночная строка
    длиннее limit режется жёстко (крайний случай, не должен происходить при
    разумном форматировании отчётов).
    """
    if len(text) <= limit:
        return [text]

    parts: list[str] = []
    lines: list[str] = []  # накопленные строки текущего чанка

    def _flush(upto: int) -> None:
        """Закрыть чанк по первым `upto` строк из `lines`, оставить остаток."""
        parts.append("\n".join(lines[:upto]))
        del lines[:upto]

    for line in text.split("\n"):
        lines.append(line)
        if len("\n".join(lines)) <= limit:
            continue

        # Не влезло — line последняя лишняя. Ищем самую позднюю пустую строку,
        # перед которой НЕ стоит голый заголовок раздела — резать сразу после
        # заголовка нельзя, иначе он останется без единого пункта списка.
        boundary = -1
        for i in range(len(lines) - 2, -1, -1):
            if lines[i] == "" and not (i > 0 and _is_bare_header_line(lines[i - 1])):
                boundary = i
                break

        if boundary > 0:
            _flush(boundary)
        elif len(lines) > 1:
            # Нет подходящей границы абзаца (весь чанк — один заголовок с
            # незавершённым списком) — режем по последней строке, которая
            # ещё помещалась (старое построчное поведение).
            _flush(len(lines) - 1)
        else:
            # Единственная строка уже длиннее limit — жёсткий рез посимвольно.
            (only,) = lines
            for i in range(0, len(only), limit):
                parts.append(only[i : i + limit])
            lines.clear()

    if lines:
        parts.append("\n".join(lines))
    return parts


async def send_admin_message(text: str) -> None:
    """Отправить сообщение всем Telegram-админам напрямую.

    Текст длиннее лимита Telegram (4096 символов) автоматически режется на
    несколько последовательных сообщений — вызывающему коду думать об этом
    не нужно.
    """
    from app.api.telegram import send_telegram_message
    from app.core.config import get_settings

    settings = get_settings()
    parts = _split_message(text)
    for admin_id in settings.admin_telegram_ids:
        for part in parts:
            try:
                await send_telegram_message(admin_id, part)
            except Exception as e:
                logger.warning("send_admin_message_failed", admin_id=admin_id, error=str(e))


async def send_admin_message_with_keyboard(text: str, buttons: list[dict]) -> None:
    """Отправить сообщение всем Telegram-админам с inline keyboard.

    buttons: [{"text": "Approve", "callback_data": "withdraw:approve:<uuid>"}, ...]
    """
    from app.api.telegram import send_telegram_with_buttons
    from app.core.config import get_settings

    settings = get_settings()
    for admin_id in settings.admin_telegram_ids:
        try:
            await send_telegram_with_buttons(admin_id, text, buttons)
        except Exception as e:
            logger.warning("send_admin_message_with_keyboard_failed", admin_id=admin_id, error=str(e))
