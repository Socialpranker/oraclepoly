"""Тесты ml_accuracy_report — логика классификации артефактов и слоёв.

Покрывает чистые функции движка (без БД) на синтетике. Главное — флаги
RELIABLE/ARTIFACT/WEAK, защищающие от ловушек концентрации (Tech&AI +18.9%
оказался артефактом: 83% сигналов на 1 событии, 100% side=NO).
"""

from __future__ import annotations

from scripts.research.ml_accuracy_report import (
    _compute_layer1,
    _compute_money,
)


def _row(**kw) -> dict:
    """Синтетическая «строка» (asyncpg.Record поддерживает []-доступ как dict)."""
    return kw


# ── Классификация артефактов ─────────────────────────────────────────────────


def test_reliable_category_diversified_positive():
    """Диверсифицированная категория с положительным ROI → RELIABLE."""
    rows = [
        _row(detector="STANDALONE_V2_LGB", category="World & Economy",
             event_id=f"ev{i}", side="YES" if i % 2 else "NO",
             realized_roi_net=5.0 + (i % 3), outcome="WIN" if i % 2 else "LOSS")
        for i in range(40)  # 40 разных событий, нет концентрации
    ]
    out = _compute_money(rows)
    assert len(out) == 1
    m = out[0]
    assert m.reliability_flag == "RELIABLE"
    assert m.n_events == 40
    assert m.event_concentration < 0.6
    assert not m.artifact_reasons


def test_artifact_concentration_on_single_event():
    """83% сигналов на 1 событии → ARTIFACT (как Tech&AI)."""
    rows = [
        _row(detector="STANDALONE_V2_LGB", category="Tech & AI",
             event_id="cerebras", side="NO",
             realized_roi_net=46.5, outcome="WIN")
        for _ in range(100)
    ]
    # 3 «других» события — но топ-событие = 100/103 = 97% концентрации
    rows += [
        _row(detector="STANDALONE_V2_LGB", category="Tech & AI",
             event_id=f"other{i}", side="NO", realized_roi_net=-52.5, outcome="LOSS")
        for i in range(3)
    ]
    out = _compute_money(rows)
    m = out[0]
    assert m.reliability_flag == "ARTIFACT"
    assert any("концентрац" in r for r in m.artifact_reasons)
    assert any("side-перекос" in r for r in m.artifact_reasons)  # 100% NO


def test_artifact_loo_sign_flip():
    """leave-one-event-out переворачивает знак ROI → ARTIFACT (как Politics)."""
    # Одно большое везучее событие тянет avg в плюс; без него — минус.
    rows = [
        _row(detector="STANDALONE_V2_LGB", category="Politics",
             event_id="lucky", side="YES", realized_roi_net=40.0, outcome="WIN")
        for _ in range(30)
    ]
    rows += [
        _row(detector="STANDALONE_V2_LGB", category="Politics",
             event_id=f"ev{i}", side="YES", realized_roi_net=-10.0, outcome="LOSS")
        for i in range(20)  # 20 событий, каждое теряет
    ]
    out = _compute_money(rows)
    m = out[0]
    # avg положителен (30×40 − 20×10 > 0), но LOO без lucky → отрицателен
    assert m.avg_roi_net_pct > 0
    assert m.loo_avg_roi_pct is not None and m.loo_avg_roi_pct < 0
    assert m.reliability_flag == "ARTIFACT"
    assert any("LOO" in r for r in m.artifact_reasons)


def test_weak_side_skew_but_diversified():
    """100% NO но диверсифицировано по событиям → WEAK (не ARTIFACT)."""
    rows = [
        _row(detector="D3_cross_mkt_arb", category="World & Economy",
             event_id=f"ev{i}", side="NO",
             realized_roi_net=5.0, outcome="WIN")
        for i in range(30)  # 30 событий → нет концентрации, знак стабилен
    ]
    out = _compute_money(rows)
    m = out[0]
    # side-перекос есть, но концентрации/LOO-flip/мало-событий — нет
    assert m.side_no_share == 1.0
    assert m.reliability_flag == "WEAK"
    assert any("side-перекос" in r for r in m.artifact_reasons)


def test_artifact_few_events():
    """Мало событий → ARTIFACT даже при положительном ROI."""
    rows = [
        _row(detector="D26_fade_whale_extreme", category="Culture",
             event_id=f"ev{i % 2}", side="NO", realized_roi_net=49.7, outcome="WIN")
        for i in range(4)  # только 2 события
    ]
    out = _compute_money(rows)
    m = out[0]
    assert m.n_events == 2
    assert m.reliability_flag == "ARTIFACT"
    assert any("мало событий" in r for r in m.artifact_reasons)


def test_median_differs_from_average_tail():
    """median vs average расходятся при хвостовом распределении (EXPIRED=0 масса)."""
    rows = [
        _row(detector="STANDALONE_V2_LGB", category="Crypto",
             event_id=f"ev{i}", side="NO",
             realized_roi_net=0.0, outcome="EXPIRED")
        for i in range(80)  # масса нулей (EXPIRED)
    ]
    rows += [
        _row(detector="STANDALONE_V2_LGB", category="Crypto",
             event_id=f"win{i}", side="NO", realized_roi_net=48.0, outcome="WIN")
        for i in range(20)
    ]
    out = _compute_money(rows)
    m = out[0]
    assert m.median_roi_net_pct == 0.0  # медиана = нули
    assert m.avg_roi_net_pct > 0  # среднее тянут wins


# ── Слой 1: честность вероятности ────────────────────────────────────────────


def test_layer1_perfect_calibration():
    """Идеально калиброванная модель: slope≈1, низкий ECE, edge над рынком."""
    # Модель = истина, рынок = шум вокруг 0.5
    rows = []
    for i in range(100):
        y = i % 2  # 50/50
        rows.append(_row(
            anchor_hours=24, event_id=f"ev{i}",
            predicted_p_yes=0.95 if y == 1 else 0.05,  # уверенная и правильная
            market_yes_price=0.5,  # рынок не знает
            yes_won=y,
        ))
    layer1, segments = _compute_layer1(rows)
    assert len(layer1) == 1
    m = layer1[0]
    assert m.per_row_brier < 0.05  # почти идеально
    assert m.edge_vs_market > 0  # бьёт рынок (market_brier=0.25)
    assert m.brier_skill_vs_market > 0
    assert m.auc is not None and m.auc > 0.99


def test_layer1_separates_anchors():
    """Метрики считаются отдельно per anchor."""
    rows = [
        _row(anchor_hours=24, event_id=f"a{i}", predicted_p_yes=0.6,
             market_yes_price=0.5, yes_won=i % 2)
        for i in range(20)
    ]
    rows += [
        _row(anchor_hours=72, event_id=f"b{i}", predicted_p_yes=0.7,
             market_yes_price=0.5, yes_won=i % 2)
        for i in range(20)
    ]
    layer1, _ = _compute_layer1(rows)
    anchors = {m.anchor_hours for m in layer1}
    assert anchors == {24, 72}
