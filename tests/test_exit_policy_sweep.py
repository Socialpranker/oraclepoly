"""Unit tests для exit-policy (TP/SL) sweep движка H1.

Синтетика, без БД. Проверяем: симуляцию одного сигнала, грид-ячейку, sweep,
выбор лучшей ячейки с sample-gate, time-split, Sortino и полный recommend
pipeline с out-of-sample validate.
"""

from __future__ import annotations

import pytest

from app.services.exit_policy_sweep import (
    SignalPath,
    _simulate_one,
    _sortino,
    best_cell,
    evaluate_grid_cell,
    recommend,
    sweep,
    time_split,
)
from app.services.shadow_simulation import ROUND_TRIP_FEE_PCT


def _path(
    *,
    signal_id: str = "sig",
    side: str = "NO",
    entry: float = 0.40,
    prices: list[float] | None = None,
    resolved: bool = True,
    outcome: str | None = "WIN",
    realized_roi_net: float | None = 0.0,
) -> SignalPath:
    """Хелпер: SignalPath из списка yes_price (captured_at — порядковый)."""
    price_path = [{"captured_at": f"t{i}", "yes_price": p} for i, p in enumerate(prices or [])]
    return SignalPath(
        signal_id=signal_id,
        side=side,
        entry_yes_price=entry,
        price_path=price_path,
        resolved=resolved,
        outcome=outcome,
        realized_roi_net=realized_roi_net,
    )


# ─── _simulate_one: trigger paths ────────────────────────────────────────────


def test_simulate_one_tp_fires_first_crossing():
    """NO @ 0.40 (cost 0.60). TP=30 fires когда cost ≥ 0.78 → yes ≤ 0.22.
    Симулятор берёт ПЕРВУЮ точку где PnL ≥ tp, pnl = tp - fees."""
    path = _path(entry=0.40, prices=[0.40, 0.30, 0.20])  # +0%, +16.7%, +33%
    reason, pnl = _simulate_one(path, tp_pct=30.0, sl_pct=-20.0)
    assert reason == "take_profit"
    assert pnl == pytest.approx(30.0 - ROUND_TRIP_FEE_PCT)


def test_simulate_one_sl_fires_first_crossing():
    """NO @ 0.20 (cost 0.80). SL=-20 fires когда cost ≤ 0.64 → yes ≥ 0.36.
    pnl = sl - fees."""
    path = _path(entry=0.20, prices=[0.20, 0.30, 0.40])  # 0%, -12.5%, -25%
    reason, pnl = _simulate_one(path, tp_pct=30.0, sl_pct=-20.0)
    assert reason == "stop_loss"
    assert pnl == pytest.approx(-20.0 - ROUND_TRIP_FEE_PCT)


def test_simulate_one_tp_checked_before_sl_within_snapshot():
    """Если одна точка пробивает и TP и SL (вырожденный случай), TP проверяется
    первым в коде → take_profit. На реальных данных не возникает, но фиксируем."""
    # YES @ 0.50, точка 0.99: pnl = (0.99-0.50)/0.50 = +98%. tp=10 проходит,
    # sl=-5 — нет (pnl положителен). Проверяем что TP-ветка срабатывает.
    path = _path(side="YES", entry=0.50, prices=[0.99])
    reason, _ = _simulate_one(path, tp_pct=10.0, sl_pct=-5.0)
    assert reason == "take_profit"


# ─── _simulate_one: no-trigger fallbacks ─────────────────────────────────────


def test_simulate_one_resolution_when_no_trigger():
    """Нет TP/SL trigger + resolved + outcome WIN → resolution, pnl = realized_roi_net (в %).
    Sweep НЕ имеет hold-to-resolution special-case — это просто отсутствие триггера."""
    path = _path(entry=0.40, prices=[0.40, 0.38], realized_roi_net=66.0, outcome="WIN")
    reason, pnl = _simulate_one(path, tp_pct=30.0, sl_pct=-20.0)
    assert reason == "resolution"
    assert pnl == pytest.approx(66.0)


def test_simulate_one_expired_zero_pnl():
    """outcome EXPIRED + нет триггера → expired, pnl = 0."""
    path = _path(prices=[0.40], outcome="EXPIRED", realized_roi_net=None)
    reason, pnl = _simulate_one(path, tp_pct=30.0, sl_pct=-20.0)
    assert reason == "expired"
    assert pnl == 0.0


def test_simulate_one_unresolved_no_data():
    """resolved=false + нет триггера → no_data, pnl = 0 (caller исключает)."""
    path = _path(prices=[0.40, 0.41], resolved=False, outcome=None, realized_roi_net=None)
    reason, pnl = _simulate_one(path, tp_pct=30.0, sl_pct=-20.0)
    assert reason == "no_data"
    assert pnl == 0.0


def test_simulate_one_no_hold_to_resolution_special_case():
    """КЛЮЧЕВОЕ отличие от simulate_signal: sweep ВСЕГДА проверяет триггеры.
    Большой move у "D38-подобного" сигнала здесь даёт TP, а не resolution."""
    # NO @ 0.05 (cost 0.95), точка 0.01 (cost 0.99): pnl = (0.99-0.95)/0.95 = +4.2%.
    # При tp=4 → TP срабатывает (в simulate_signal D38 ушёл бы в resolution).
    path = _path(entry=0.05, prices=[0.01], realized_roi_net=4.0, outcome="WIN")  # roi в % (не используется — TP)
    reason, pnl = _simulate_one(path, tp_pct=4.0, sl_pct=-40.0)
    assert reason == "take_profit"
    assert pnl == pytest.approx(4.0 - ROUND_TRIP_FEE_PCT)


# ─── evaluate_grid_cell ──────────────────────────────────────────────────────


def test_evaluate_grid_cell_excludes_no_data():
    """no_data не считается closed: n_signals и PnL только по закрытым."""
    paths = [
        _path(signal_id="tp", entry=0.40, prices=[0.20]),  # TP
        _path(signal_id="nodata", prices=[0.40], resolved=False, outcome=None),  # no_data
    ]
    cell = evaluate_grid_cell(paths, tp_pct=30.0, sl_pct=-20.0)
    assert cell.n_signals == 1  # no_data исключён
    assert cell.n_tp == 1
    assert cell.sum_pnl_pct == pytest.approx(30.0 - ROUND_TRIP_FEE_PCT)


def test_evaluate_grid_cell_counts_all_reasons():
    """Смесь TP/SL/resolution/expired/no_data → корректные счётчики."""
    paths = [
        _path(signal_id="tp", entry=0.40, prices=[0.20]),  # TP +26
        _path(signal_id="sl", entry=0.20, prices=[0.40]),  # SL -24
        _path(signal_id="res", entry=0.40, prices=[0.39], realized_roi_net=10.0),  # res +10 (в %)
        _path(signal_id="exp", prices=[0.40], outcome="EXPIRED", realized_roi_net=None),  # exp 0
        _path(signal_id="nd", prices=[0.40], resolved=False, outcome=None),  # no_data
    ]
    cell = evaluate_grid_cell(paths, tp_pct=30.0, sl_pct=-20.0)
    assert cell.n_signals == 4  # no_data исключён
    assert cell.n_tp == 1
    assert cell.n_sl == 1
    assert cell.n_resolution == 1
    assert cell.n_expired == 1
    # sum = 26 - 24 + 10 + 0 = 12
    assert cell.sum_pnl_pct == pytest.approx(12.0)
    # mean = 12 / 4 = 3.0
    assert cell.mean_pnl_pct == pytest.approx(3.0)


def test_evaluate_grid_cell_all_no_data_mean_none():
    """Все no_data → n_signals=0, mean_pnl_pct None (нечего усреднять)."""
    paths = [_path(prices=[0.40], resolved=False, outcome=None) for _ in range(3)]
    cell = evaluate_grid_cell(paths, tp_pct=30.0, sl_pct=-20.0)
    assert cell.n_signals == 0
    assert cell.mean_pnl_pct is None
    assert cell.sortino is None


# ─── sweep ───────────────────────────────────────────────────────────────────


def test_sweep_produces_full_grid():
    """sweep возвращает len(tp_grid) × len(sl_grid) ячеек."""
    paths = [_path(entry=0.40, prices=[0.20])]
    cells = sweep(paths, tp_grid=(10.0, 20.0, 30.0), sl_grid=(-10.0, -20.0))
    assert len(cells) == 3 * 2
    # Каждая ячейка несёт свою пару (tp, sl).
    pairs = {(c.tp_pct, c.sl_pct) for c in cells}
    assert pairs == {(10.0, -10.0), (10.0, -20.0), (20.0, -10.0), (20.0, -20.0), (30.0, -10.0), (30.0, -20.0)}


# ─── best_cell: objective + sample-gate ──────────────────────────────────────


def test_best_cell_sample_gate_filters_small_n():
    """Ячейки с n_signals < min_signals игнорируются. Если все ниже gate → None."""
    paths = [_path(entry=0.40, prices=[0.20])]  # 1 сигнал
    cells = sweep(paths, tp_grid=(30.0,), sl_grid=(-20.0,))
    assert best_cell(cells, min_signals=30) is None  # 1 < 30
    assert best_cell(cells, min_signals=1) is not None  # gate проходит


def test_best_cell_maximizes_mean_pnl():
    """objective=mean_pnl выбирает ячейку с наибольшим mean."""
    # 30 одинаковых TP-сигналов: высокий tp = высокий mean (tp - fees).
    paths = [_path(signal_id=f"s{i}", side="YES", entry=0.10, prices=[0.99]) for i in range(30)]
    cells = sweep(paths, tp_grid=(10.0, 50.0), sl_grid=(-40.0,))
    best = best_cell(cells, objective="mean_pnl", min_signals=30)
    assert best is not None
    assert best.tp_pct == 50.0  # 50-4=46 > 10-4=6


def test_best_cell_sum_pnl_objective():
    """objective=sum_pnl ранжирует по сумме, не среднему."""
    paths = [_path(signal_id=f"s{i}", side="YES", entry=0.10, prices=[0.99]) for i in range(30)]
    cells = sweep(paths, tp_grid=(10.0, 50.0), sl_grid=(-40.0,))
    best = best_cell(cells, objective="sum_pnl", min_signals=30)
    assert best is not None
    assert best.tp_pct == 50.0


def test_best_cell_none_when_no_cells():
    assert best_cell([], min_signals=1) is None


# ─── _sortino ────────────────────────────────────────────────────────────────


def test_sortino_none_below_two_points():
    assert _sortino([5.0]) is None
    assert _sortino([]) is None


def test_sortino_none_when_no_downside():
    """Все PnL ≥ 0 → downside-deviation = 0 → None (коэффициент бесконечен)."""
    assert _sortino([10.0, 20.0, 30.0]) is None


def test_sortino_positive_with_downside():
    """Смесь + и − → конечный Sortino. mean / dd, dd = sqrt(mean(min(0,p)^2))."""
    pnls = [10.0, 10.0, -10.0, -10.0]
    # mean = 0; sortino = 0 / dd = 0 (mean ноль, но dd>0).
    s = _sortino(pnls)
    assert s is not None
    assert s == pytest.approx(0.0)


def test_sortino_rewards_higher_mean_same_downside():
    """При одинаковом downside выше mean → выше Sortino."""
    low = _sortino([5.0, -10.0])
    high = _sortino([50.0, -10.0])
    assert low is not None and high is not None
    assert high > low


# ─── time_split ──────────────────────────────────────────────────────────────


def test_time_split_default_fraction():
    """train_frac=0.6 на 10 сигналах → 6 train / 4 validate, порядок сохранён."""
    paths = [_path(signal_id=f"s{i}") for i in range(10)]
    train, validate = time_split(paths, train_frac=0.6)
    assert len(train) == 6
    assert len(validate) == 4
    assert [p.signal_id for p in train] == [f"s{i}" for i in range(6)]
    assert [p.signal_id for p in validate] == [f"s{i}" for i in range(6, 10)]


def test_time_split_empty():
    assert time_split([]) == ([], [])


def test_time_split_at_least_one_in_train():
    """Маленький набор: cut = max(1, ...) — train не пустой."""
    paths = [_path(signal_id="a"), _path(signal_id="b")]
    train, validate = time_split(paths, train_frac=0.1)
    assert len(train) == 1
    assert len(validate) == 1


# ─── recommend: полный pipeline ──────────────────────────────────────────────


def test_recommend_insufficient_data_when_below_gate():
    """Мало сигналов на train → status insufficient_data, рекомендации None."""
    paths = [_path(signal_id=f"s{i}", entry=0.40, prices=[0.20]) for i in range(5)]
    out = recommend(paths, min_signals=30)
    assert out["status"] == "insufficient_data"
    assert out["recommended_tp"] is None
    assert out["recommended_sl"] is None


def test_recommend_ok_returns_oos_validate():
    """Достаточно данных → status ok, рекомендация + out-of-sample validate-метрика.
    50 одинаковых TP-сигналов: train=30, validate=20, та же пара проверяется на validate."""
    paths = [_path(signal_id=f"s{i}", side="YES", entry=0.10, prices=[0.99]) for i in range(50)]
    out = recommend(
        paths,
        tp_grid=(10.0, 50.0),
        sl_grid=(-40.0,),
        objective="mean_pnl",
        min_signals=30,
        train_frac=0.6,
    )
    assert out["status"] == "ok"
    assert out["n_train"] == 30
    assert out["n_validate"] == 20
    assert out["recommended_tp"] == 50.0
    # Out-of-sample: те же 20 TP-сигналов на validate.
    assert out["validate_n_signals"] == 20
    assert out["validate_mean_pnl_pct"] == pytest.approx(50.0 - ROUND_TRIP_FEE_PCT)


def test_recommend_does_not_mutate_or_autoapply():
    """recommend — чистая функция: возвращает dict-рекомендацию, ничего не применяет.
    Объект paths не меняется (frozen dataclasses)."""
    paths = [_path(signal_id=f"s{i}", side="YES", entry=0.10, prices=[0.99]) for i in range(40)]
    before = [p.signal_id for p in paths]
    out = recommend(paths, tp_grid=(30.0,), sl_grid=(-20.0,), min_signals=30)
    assert [p.signal_id for p in paths] == before
    assert "recommended_tp" in out  # рекомендация, а не side-effect
