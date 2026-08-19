"""Тесты рендерера уведомлений — app/analytics/notification_render.py.

Notification.what_happened/.actions/.evidence хранятся как tuple, но конструктор
принимает list — используем list на входе (естественно для вызывающего кода).
"""

from __future__ import annotations

from app.analytics.notification import ActionSpec, Notification, Severity
from app.analytics.notification_render import render


def _n(**over):
    kw = dict(
        severity=Severity.ACTION_REQUIRED,
        subject="detector:D38_resolution_lag",
        kind="edge_decay",
        headline="Преимущество детектора тает",
        what_happened=["Точность держится (~90% верных), но доходность ушла в минус."],
        what_if_ignored="детектор в тени, реальных денег не теряешь",
        what_to_do="Нажми 🔍 — отметить к разбору.",
        actions=[],
        evidence=["run #533", "n=2476 за период"],
    )
    kw.update(over)
    return Notification(**kw)


def test_headline_and_human_subject_present():
    html, _ = render(_n(), pid=None)
    assert "Преимущество детектора тает" in html
    assert "D38 «Задержка резолва»" in html
    assert "D38_resolution_lag" not in html.split("основание")[0]


def test_what_if_ignored_omitted_when_none():
    html, _ = render(_n(what_if_ignored=None), pid=None)
    assert "Если ничего не делать" not in html


def test_what_if_ignored_present_when_given():
    html, _ = render(_n(), pid=None)
    assert "Если ничего не делать" in html


def test_evidence_rendered_last():
    html, _ = render(_n(), pid=None)
    assert html.rstrip().endswith("run #533 · n=2476 за период</i>")


def test_html_is_escaped():
    """Риск №6 спеки: имена попадают в HTML."""
    html, _ = render(_n(headline="<script>alert(1)</script>"), pid=None)
    assert "<script>" not in html
    assert "&lt;script&gt;" in html


def test_no_keyboard_without_pid():
    _, rows = render(_n(), pid=None)
    assert rows == []


def test_keyboard_with_execute_when_action_present():
    action = ActionSpec(kind="pause_detector", payload={"detector": "D38"}, snapshot={"stage": "shadow"})
    _, rows = render(_n(actions=[action]), pid="abc123")
    flat = [b["callback_data"] for row in rows for b in row]
    assert "act:abc123" in flat


def test_keyboard_without_execute_when_no_action():
    _, rows = render(_n(), pid="abc123")
    flat = [b["callback_data"] for row in rows for b in row]
    assert "act:abc123" not in flat
    assert any(cb.startswith("adv:") for cb in flat)


def test_telegram_length_limit_respected():
    """Риск №3 спеки: склеенное сообщение не должно превышать лимит TG."""
    long_lines = [f"Строка номер {i} с довольно длинным текстом про метрики." for i in range(200)]
    html, _ = render(_n(what_happened=long_lines), pid=None)
    assert len(html) <= 4096
    assert "…" in html


def test_truncation_never_breaks_html_tags():
    """Обрезка не смеет разорвать тег.

    Посимвольная обрезка давала `<b>Кто:<` вместо `<b>Кто:</b>` — Telegram отвергает
    битую разметку целиком, то есть уведомление не дошло бы вообще. Перебираем длины
    вокруг лимита: разрыв возникал только на конкретных, «удобная» длина его прячет.
    """
    for pad in range(3900, 4200, 3):
        html, _ = render(_n(headline="X" * pad), pid=None)
        assert len(html) <= 4096, f"превышен лимит на pad={pad}"
        assert html.count("<b>") == html.count("</b>"), f"разорван тег <b> на pad={pad}: {html[-50:]!r}"
        assert html.count("<i>") == html.count("</i>"), f"разорван тег <i> на pad={pad}: {html[-50:]!r}"


def test_single_line_longer_than_limit_is_dropped_whole():
    """Строка, не влезающая целиком, отбрасывается, а не калечится посередине тега."""
    html, _ = render(_n(what_happened=["Я" * 9000]), pid=None)
    assert len(html) <= 4096
    assert html.count("<b>") == html.count("</b>")
    assert "…(сообщение обрезано)" in html
