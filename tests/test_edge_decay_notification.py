"""Tests для app.analytics.edge_decay_alert_formatter.to_notifications."""

from __future__ import annotations

from app.analytics.changepoint import ChangepointResult
from app.analytics.edge_decay_alert_formatter import EntityDecayResult, to_notifications
from app.analytics.edge_metrics import WindowMetrics
from app.analytics.notification import Severity


def _cp(triggered=True, direction="below", stat=8.8):
    return ChangepointResult(cusum_statistic=stat, threshold=5.0, triggered=triggered, direction=direction)


def _wm(wl=0.900, wu=0.912, sortino=-0.05, n=2476):
    # wins — обязательное поле WindowMetrics (edge_metrics.py:42), не опускать.
    return WindowMetrics(n=n, wins=int(n * wl), wilson_lower=wl, wilson_upper=wu, sortino=sortino)


def _entity(**over):
    kw = dict(
        entity_name="D38_resolution_lag",
        entity_kind="detector",
        baseline=_wm(sortino=-0.001, n=8897),
        recent=_wm(),
        wilson_changepoint=_cp(),
        sortino_changepoint=_cp(stat=51.3),
    )
    kw.update(over)
    return EntityDecayResult(**kw)


def test_decaying_entity_produces_notification():
    out = to_notifications([_entity()])
    assert len(out) == 1
    assert out[0].subject == "detector:D38_resolution_lag"
    assert out[0].kind == "edge_decay"


def test_clean_entity_produces_nothing():
    clean = _entity(wilson_changepoint=_cp(triggered=False), sortino_changepoint=_cp(triggered=False))
    assert to_notifications([clean]) == []


def test_raw_cusum_goes_to_evidence_not_body():
    """Сырые S=... не должны быть в теле — только в основании (спека §5)."""
    n = to_notifications([_entity()])[0]
    body = " ".join(n.what_happened)
    assert "CUSUM" not in body
    assert "S=" not in body
    assert any("CUSUM" in e for e in n.evidence)


def test_shadow_detector_has_no_money_forecast():
    """Спека §3: выдумывать цифру убытка для детектора не в live запрещено."""
    n = to_notifications([_entity()])[0]
    assert n.what_if_ignored is not None
    assert "$" not in n.what_if_ignored


def test_notification_carries_next_step():
    n = to_notifications([_entity()])[0]
    assert n.severity == Severity.ACTION_REQUIRED
    assert n.what_to_do


def test_no_execute_action_offered_for_shadow_detector():
    """Детектор не в live — паузить нечего, кнопки исполнения нет.

    Notification.__post_init__ нормализует actions в tuple (настоящая
    неизменяемость, app/analytics/notification.py) — сравниваем с (), не с [].
    """
    assert to_notifications([_entity()])[0].actions == ()


def test_accuracy_shown_to_tenths_not_rounded_to_whole():
    """Округление до целых схлопывало 90.0% и 90.2% в одинаковые «90%».

    Владелец читал «было 90%, стало 90%» при реальном сдвиге — вывод выглядел
    как «ничего не изменилось», хотя сообщение пришло именно про изменение.
    Цифры — из настоящего алерта 2026-08-04, с которого началась переделка.
    """
    entity = _entity(baseline=_wm(wl=0.900, sortino=-0.001, n=8897), recent=_wm(wl=0.902, n=2476))
    body = " ".join(to_notifications([entity])[0].what_happened)
    assert "90.0%" in body and "90.2%" in body
    assert "было 90%, стало 90%" not in body


def test_metric_divergence_is_named_explicitly():
    """Точность держится, доходность падает — это ровно тот вопрос владельца
    («winrate вырос, а Sortino упал — что это значит?»), и сообщение обязано
    ответить на него само, а не оставлять читателя гадать."""
    body = " ".join(to_notifications([_entity()])[0].what_happened)
    assert "расходятся" in body
    assert "цене ошибок" in body


def test_no_negative_zero_in_numbers():
    """«-0.00» читается как опечатка и подрывает доверие к цифрам."""
    entity = _entity(baseline=_wm(sortino=-0.0001, n=8897))
    body = " ".join(to_notifications([entity])[0].what_happened)
    assert "-0.00" not in body
