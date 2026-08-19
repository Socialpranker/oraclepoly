"""Tests для app.analytics.adverse_selection_formatter."""

from __future__ import annotations

from app.analytics.adverse_selection import AdverseCell
from app.analytics.adverse_selection_formatter import format_alert, to_notifications
from app.analytics.notification import Severity


def _cell(detector, category, *, is_temperature=False, is_adverse=True):
    return AdverseCell(
        detector=detector, category=category, n_events=30, wins=11,
        wilson_lower=0.37, wilson_upper=0.55, net_pnl=-88.0,
        is_temperature=is_temperature, is_adverse=is_adverse,
    )


def test_format_alert_lists_adverse_cells_with_gate_recommendation():
    cells = [_cell("D38", "World & Economy")]
    text = format_alert(cells, added=["D38×World & Economy"], removed=[])
    assert "D38×World & Economy" in text
    assert "-88" in text or "-$88" in text
    # gate-рекомендация текстом (не auto-action)
    assert "блэклист" in text.lower() or "gate" in text.lower()


def test_format_alert_includes_knowledge_ref():
    cells = [_cell("D38", "World & Economy")]
    text = format_alert(cells, added=["D38×World & Economy"], removed=[])
    assert "market_economics.md" in text


def test_format_alert_marks_temperature_anchor():
    cells = [_cell("D38", "World & Economy", is_temperature=True)]
    text = format_alert(cells, added=["D38×World & Economy"], removed=[])
    assert "temperature" in text.lower() or "🌡" in text


def test_format_alert_renders_added_and_removed_deltas():
    cells = [_cell("D38", "World & Economy")]
    text = format_alert(
        cells, added=["D38×World & Economy"], removed=["D3×Sports"],
    )
    assert "D3×Sports" in text  # removed показывается для контекста


def test_format_alert_shows_only_adverse_cells():
    cells = [_cell("D38", "World & Economy"), _cell("D3", "Crypto", is_adverse=False)]
    text = format_alert(cells, added=["D38×World & Economy"], removed=[])
    assert "D38×World & Economy" in text
    assert "D3×Crypto" not in text  # не-adverse не показываем


def test_added_and_removed_use_different_kinds():
    """Появление и уход из adverse обязаны иметь разный kind: cooldown шины —
    25 часов по (subject, kind), и общий kind подавил бы «связка перестала быть
    убыточной» после «связка стала убыточной». Та же ловушка, что с recovery
    у proxy_health_check."""
    cells = [_cell("D38", "World & Economy")]
    ns = to_notifications(cells, added=["D38×World & Economy"], removed=["D3×Sports"])

    kinds = {n.kind for n in ns}
    assert len(kinds) == 2
    assert len({n.dedup_key() for n in ns}) == 2


def test_removed_is_fyi_without_next_step():
    ns = to_notifications([], added=[], removed=["D3×Sports"])

    assert ns[0].severity == Severity.FYI
    assert ns[0].what_to_do is None


def test_money_figure_is_named_virtual():
    """net_pnl считается по execution_mode='virtual'. Подать бумажный убыток как
    живые деньги — то же враньё, что выдумать цифру, только правдоподобнее."""
    cells = [_cell("D38", "World & Economy")]
    n = to_notifications(cells, added=["D38×World & Economy"], removed=[])[0]

    assert n.what_if_ignored is not None
    assert "виртуальн" in n.what_if_ignored.lower()
    assert "-88" in n.what_if_ignored


def test_detector_name_is_humanized():
    """Спека §1 п.2: владельцу — человеческое имя, машинное в основание."""
    cells = [_cell("D3_cross_mkt_arb", "Crypto")]
    n = to_notifications(cells, added=["D3_cross_mkt_arb×Crypto"], removed=[])[0]

    assert "Расхождение цен" in " ".join(n.what_happened)
    assert any("D3_cross_mkt_arb×Crypto" in e for e in n.evidence)


def test_nothing_changed_yields_no_notification():
    assert to_notifications([_cell("D38", "World & Economy")], added=[], removed=[]) == []
