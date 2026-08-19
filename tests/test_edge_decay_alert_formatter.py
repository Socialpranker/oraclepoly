"""Tests для app.analytics.edge_decay_alert_formatter."""

from __future__ import annotations

from app.analytics.changepoint import ChangepointResult
from app.analytics.edge_decay_alert_formatter import EntityDecayResult, format_alert, is_decaying
from app.analytics.edge_metrics import WindowMetrics

_NOT_TRIGGERED = ChangepointResult(triggered=False, cusum_statistic=0.0, threshold=5.0, direction=None)


def _metrics(n=100, wins=68, lower=0.61, upper=0.75, sortino=1.42) -> WindowMetrics:
    return WindowMetrics(n=n, wins=wins, wilson_lower=lower, wilson_upper=upper, sortino=sortino)


def test_format_alert_empty_list_returns_empty_string():
    assert format_alert([]) == ""


def test_format_alert_no_triggered_entities_returns_empty_string():
    entity = EntityDecayResult(
        entity_name="D3_cross_mkt_arb",
        entity_kind="detector",
        baseline=_metrics(),
        recent=_metrics(),
        wilson_changepoint=_NOT_TRIGGERED,
        sortino_changepoint=_NOT_TRIGGERED,
    )

    assert format_alert([entity]) == ""


def test_format_alert_triggered_entity_includes_key_numbers():
    triggered = ChangepointResult(triggered=True, cusum_statistic=6.8, threshold=5.0, direction="below")
    entity = EntityDecayResult(
        entity_name="D38_resolution_lag",
        entity_kind="detector",
        baseline=_metrics(n=142, wins=97, lower=0.61, upper=0.75, sortino=1.42),
        recent=_metrics(n=24, wins=10, lower=0.29, upper=0.55, sortino=-0.31),
        wilson_changepoint=triggered,
        sortino_changepoint=_NOT_TRIGGERED,
    )

    text = format_alert([entity])

    assert "D38_resolution_lag" in text
    assert "142" in text
    assert "24" in text
    assert "6.8" in text


def test_format_alert_multiple_triggered_entities_all_present():
    triggered = ChangepointResult(triggered=True, cusum_statistic=6.0, threshold=5.0, direction="below")
    entities = [
        EntityDecayResult(
            entity_name=name,
            entity_kind="detector",
            baseline=_metrics(),
            recent=_metrics(),
            wilson_changepoint=triggered,
            sortino_changepoint=_NOT_TRIGGERED,
        )
        for name in ("D3_cross_mkt_arb", "D38_resolution_lag")
    ]

    text = format_alert(entities)

    assert "D3_cross_mkt_arb" in text
    assert "D38_resolution_lag" in text


def test_is_decaying_true_when_wilson_below_triggered():
    triggered_below = ChangepointResult(triggered=True, cusum_statistic=6.0, threshold=5.0, direction="below")
    entity = EntityDecayResult(
        entity_name="D3_cross_mkt_arb",
        entity_kind="detector",
        baseline=_metrics(),
        recent=_metrics(),
        wilson_changepoint=triggered_below,
        sortino_changepoint=_NOT_TRIGGERED,
    )

    assert is_decaying(entity) is True


def test_is_decaying_false_when_triggered_but_above():
    triggered_above = ChangepointResult(triggered=True, cusum_statistic=6.0, threshold=5.0, direction="above")
    entity = EntityDecayResult(
        entity_name="D3_cross_mkt_arb",
        entity_kind="detector",
        baseline=_metrics(),
        recent=_metrics(),
        wilson_changepoint=triggered_above,
        sortino_changepoint=_NOT_TRIGGERED,
    )

    assert is_decaying(entity) is False


def test_is_decaying_false_when_nothing_triggered():
    entity = EntityDecayResult(
        entity_name="D3_cross_mkt_arb",
        entity_kind="detector",
        baseline=_metrics(),
        recent=_metrics(),
        wilson_changepoint=_NOT_TRIGGERED,
        sortino_changepoint=_NOT_TRIGGERED,
    )

    assert is_decaying(entity) is False
