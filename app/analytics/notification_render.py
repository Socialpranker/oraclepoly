"""Notification → (HTML, клавиатура). Чистая функция, без I/O (спека §6).

Порядок блоков наследует формат advisory (спека advisory-actionable-text §3):
заголовок → кто → что случилось → цена бездействия → что делать → основание.
Сырые метрики уходят в evidence последней строкой: владельцу нужен вывод,
отладочные цифры — справочно.
"""

from __future__ import annotations

import html as html_mod

from app.analytics.advisory_buttons import build_advisory_keyboard
from app.analytics.notification import Notification, Severity
from app.analytics.subject_labels import humanize_subject

TELEGRAM_LIMIT = 4096

_SEVERITY_EMOJI = {
    Severity.ACTION_REQUIRED: "⚠️",
    Severity.FYI: "ℹ️",
    Severity.TELEMETRY: "·",
}

_BUTTON_HINT = "✅ = «посмотрел» · 🔍 = отметить к разбору"


def _esc(text: str) -> str:
    return html_mod.escape(str(text))


def _truncate(parts: list[str]) -> str:
    """Обрезка по лимиту TG с явным маркером. Молчаливая потеря хвоста
    недопустима — владелец должен видеть, что текст неполон.

    Режем по ГРАНИЦАМ СТРОК, а не посимвольно: каждая строка — законченный HTML,
    и выброс целых строк тегов не рвёт. Посимвольная обрезка давала `<b>Кто:<`
    вместо `<b>Кто:</b>`, а Telegram отвергает битую разметку целиком — то есть
    уведомление не дошло бы вообще (named-риск спеки №3).
    """
    text = "\n".join(parts)
    if len(text) <= TELEGRAM_LIMIT:
        return text

    marker = "…(сообщение обрезано)"
    budget = TELEGRAM_LIMIT - len(marker) - 1  # -1 на перевод строки перед маркером
    kept: list[str] = []
    used = 0
    for line in parts:
        need = len(line) + (1 if kept else 0)
        if used + need > budget:
            break
        kept.append(line)
        used += need

    # Одна строка длиннее всего бюджета: режем её саму, но по ТЕКСТУ внутри тегов
    # обрезка всё равно опасна — поэтому строку целиком отбрасываем, а не калечим.
    return "\n".join([*kept, marker])


def render_digest_line(n: Notification) -> str:
    """Однострочная форма для FYI-очереди, которую забирает Daily Brief (§4).

    Отдельная функция, а не срез render(): в дайджесте помещаются десятки строк,
    и полный конверт (заголовок + «Кто» + блоки + основание) превратил бы бриф
    в простыню — ровно то, от чего уводит §4 («FYI строкой в Daily Brief»).

    Экранируется здесь же: строка кладётся в БД уже готовой к вставке в HTML
    сообщение брифа, и потребитель не обязан помнить про escape (риск №6 §10).
    """
    who = _esc(humanize_subject(n.subject))
    head = _esc(n.headline)
    # Первый блок what_happened несёт цифру; остальные — расшифровку, в строку
    # дайджеста они не влезают и остаются доступны в notification_fyi_queue.
    detail = _esc(n.what_happened[0]) if n.what_happened else ""
    line = f"{_SEVERITY_EMOJI[Severity.FYI]} <b>{who}</b>: {head}"
    return f"{line} — {detail}" if detail else line


def render(n: Notification, pid: str | None) -> tuple[str, list[list[dict]]]:
    """HTML + клавиатура. pid=None → без кнопок (FYI-строки, дайджест)."""
    emoji = _SEVERITY_EMOJI.get(n.severity, "•")
    parts = [
        f"{emoji} <b>{_esc(n.headline)}</b>",
        f"<b>Кто:</b> {_esc(humanize_subject(n.subject))}",
        "",
    ]
    parts.extend(_esc(line) for line in n.what_happened)

    if n.what_if_ignored:
        parts += ["", f"⏳ <b>Если ничего не делать:</b> {_esc(n.what_if_ignored)}"]
    if n.what_to_do:
        parts += ["", f"🔍 <b>Что делать:</b> {_esc(n.what_to_do)}"]

    rows: list[list[dict]] = []
    if pid:
        rows = build_advisory_keyboard(pid, with_execute=bool(n.actions))
        parts += ["", f"<i>{_BUTTON_HINT}</i>"]

    if n.evidence:
        parts.append(f"<i>основание: {_esc(' · '.join(n.evidence))}</i>")

    return _truncate(parts), rows
