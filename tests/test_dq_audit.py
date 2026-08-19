"""Тесты чистого DQ-слоя (Phase 2 C0). Без БД — только классификация."""

from app.analytics.dq_audit import (
    VERDICT_AMBER,
    VERDICT_GREEN,
    VERDICT_RED,
    DqThresholds,
    classify_expired_share,
    classify_freshness,
    classify_share,
    overall_verdict,
)

_PARAMS = {
    "freshness_markets_amber_minutes": 120,
    "freshness_markets_red_minutes": 720,
    "freshness_signals_amber_minutes": 360,
    "freshness_signals_red_minutes": 1440,
    "completeness_end_date_amber_pct": 15.0,
    "completeness_end_date_red_pct": 40.0,
    "completeness_tokens_amber_pct": 5.0,
    "completeness_tokens_red_pct": 20.0,
    "expired_share_amber_pct": 30.0,
    "expired_share_red_pct": 60.0,
    "expired_min_resolved": 50,
    "ri_orphan_amber_pct": 1.0,
    "ri_orphan_red_pct": 5.0,
    "lookback_days": 7,
}


def _thresholds() -> DqThresholds:
    return DqThresholds.from_params(_PARAMS)


def test_from_params_maps_all_fields():
    t = _thresholds()
    assert t.freshness_markets_red_minutes == 720
    assert t.expired_min_resolved == 50
    assert t.lookback_days == 7


def test_freshness_green_below_amber():
    r = classify_freshness(30.0, 120.0, 720.0, source="markets")
    assert r.verdict == VERDICT_GREEN
    assert r.check == "freshness:markets"


def test_freshness_amber_between():
    assert classify_freshness(300.0, 120.0, 720.0, source="markets").verdict == VERDICT_AMBER


def test_freshness_red_above():
    assert classify_freshness(9999.0, 120.0, 720.0, source="markets").verdict == VERDICT_RED


def test_freshness_none_is_red():
    r = classify_freshness(None, 120.0, 720.0, source="signals")
    assert r.verdict == VERDICT_RED
    assert "пуст" in r.detail


def test_share_boundaries_are_inclusive_red():
    # ровно на red-пороге → RED (fail-closed)
    assert classify_share("ri:orphans", 5.0, 1.0, 5.0, "x").verdict == VERDICT_RED
    assert classify_share("ri:orphans", 1.0, 1.0, 5.0, "x").verdict == VERDICT_AMBER
    assert classify_share("ri:orphans", 0.0, 1.0, 5.0, "x").verdict == VERDICT_GREEN


def test_expired_share_insufficient_n_is_green():
    r = classify_expired_share(expired=40, resolved_total=40, thresholds=_thresholds())
    assert r.verdict == VERDICT_GREEN
    assert "недостаточно" in r.detail


def test_expired_share_red_when_all_expired():
    r = classify_expired_share(expired=100, resolved_total=100, thresholds=_thresholds())
    assert r.verdict == VERDICT_RED  # 100% ≥ 60% — маркер M2/D45-класса бага


def test_overall_verdict_priority():
    green = classify_share("a", 0.0, 10.0, 20.0, "x")
    amber = classify_share("b", 15.0, 10.0, 20.0, "x")
    red = classify_share("c", 25.0, 10.0, 20.0, "x")
    assert overall_verdict([green, green]) == VERDICT_GREEN
    assert overall_verdict([green, amber]) == VERDICT_AMBER
    assert overall_verdict([amber, red]) == VERDICT_RED
    assert overall_verdict([]) == VERDICT_RED  # нет проверок = нет данных = fail-closed
