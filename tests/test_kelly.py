"""Тесты Kelly criterion — kelly_fraction + run_kelly_simulation."""

from app.core.kelly import kelly_fraction, run_kelly_simulation


def test_kelly_positive_edge():
    wr, avg_w, avg_l = 0.85, 0.90, 0.10
    f = kelly_fraction(wr, avg_w, avg_l)
    assert 0.05 < f <= 0.25
    assert isinstance(f, float)


def test_kelly_no_edge():
    f = kelly_fraction(0.5, 1.0, 1.0)
    assert f == 0.0


def test_kelly_capped_at_25():
    f = kelly_fraction(0.99, 10.0, 0.1)
    assert f == 0.25


def test_kelly_negative_edge():
    f = kelly_fraction(0.3, 0.5, 0.5)
    assert f == 0.0


def test_kelly_boundary_zero_win_rate():
    f = kelly_fraction(0.0, 1.0, 1.0)
    assert f == 0.0


def test_kelly_boundary_one_win_rate():
    f = kelly_fraction(1.0, 1.0, 1.0)
    assert f == 0.0


def test_kelly_zero_avg_loss():
    f = kelly_fraction(0.5, 1.0, 0.0)
    assert f == 0.0


def test_kelly_realistic_fade_whale():
    f = kelly_fraction(0.85, 0.935, 0.065)
    assert 0.05 < f <= 0.25


def test_run_kelly_simulation_basic():
    trades = [
        {
            "pnl_per_unit": 0.3,
            "prediction_correct": True,
            "market_price": 0.7,
            "created_at": "2026-03-01",
            "market_id": "1",
            "question": "Test?",
            "category": "Politics",
        },
        {
            "pnl_per_unit": -0.6,
            "prediction_correct": False,
            "market_price": 0.6,
            "created_at": "2026-03-02",
            "market_id": "2",
            "question": "Test2?",
            "category": "Crypto",
        },
        {
            "pnl_per_unit": 0.2,
            "prediction_correct": True,
            "market_price": 0.8,
            "created_at": "2026-03-03",
            "market_id": "3",
            "question": "Test3?",
            "category": "Politics",
        },
    ]
    enriched, equity, summary = run_kelly_simulation(trades, 1000.0)
    assert len(enriched) == 3
    assert len(equity) == 3
    assert summary["initial_bankroll"] == 1000.0
    assert summary["total_trades"] == 3
    assert summary["wins"] == 2
    assert summary["losses"] == 1
    assert summary["final_bankroll"] > 0


def test_run_kelly_simulation_empty():
    enriched, equity, summary = run_kelly_simulation([], 1000.0)
    assert enriched == []
    assert equity == []
    assert summary["total_trades"] == 0
    assert summary["final_bankroll"] == 1000.0
