"""Unit tests для _bucketize_settle_hours.

Проверяет распределение часов settled по корзинам 24h/1-3d/3-7d/>7d,
median и threshold n_min для статистической достоверности.
"""

from app.api.bot import SETTLE_VELOCITY_MIN_N, _bucketize_settle_hours


def test_empty_input_returns_zero_n() -> None:
    res = _bucketize_settle_hours([])
    assert res == {"buckets": None, "median_hours": None, "n": 0}


def test_below_min_n_returns_no_buckets() -> None:
    # n=4 < SETTLE_VELOCITY_MIN_N=5 → buckets=None, n возвращается
    res = _bucketize_settle_hours([1.0, 2.0, 3.0, 4.0])
    assert res["buckets"] is None
    assert res["median_hours"] is None
    assert res["n"] == 4


def test_min_n_threshold_constant() -> None:
    # Sanity check — threshold не должен сдвинуться без миграции UI hint
    assert SETTLE_VELOCITY_MIN_N == 5


def test_all_under_24h() -> None:
    res = _bucketize_settle_hours([1.0, 5.0, 10.0, 18.0, 23.5])
    assert res["n"] == 5
    assert res["buckets"]["under_24h_pct"] == 100.0
    assert res["buckets"]["d1_3_pct"] == 0.0
    assert res["buckets"]["d3_7_pct"] == 0.0
    assert res["buckets"]["over_7d_pct"] == 0.0
    assert res["median_hours"] == 10.0


def test_mixed_distribution() -> None:
    # 2 × <24h, 2 × 1-3d, 1 × 3-7d, 1 × >7d → 6 total
    res = _bucketize_settle_hours([5.0, 20.0, 30.0, 60.0, 100.0, 200.0])
    assert res["n"] == 6
    assert res["buckets"]["under_24h_pct"] == round(2 / 6 * 100, 1)
    assert res["buckets"]["d1_3_pct"] == round(2 / 6 * 100, 1)
    assert res["buckets"]["d3_7_pct"] == round(1 / 6 * 100, 1)
    assert res["buckets"]["over_7d_pct"] == round(1 / 6 * 100, 1)
    # median для even n = avg(30, 60) = 45
    assert res["median_hours"] == 45.0


def test_bucket_boundaries_inclusive_lower() -> None:
    # 24.0 → d1_3 (не under_24h)
    # 72.0 → d3_7 (не d1_3)
    # 168.0 → over_7d (не d3_7)
    res = _bucketize_settle_hours([23.99, 24.0, 71.99, 72.0, 167.99, 168.0])
    b = res["buckets"]
    assert b["under_24h_pct"] == round(1 / 6 * 100, 1)
    assert b["d1_3_pct"] == round(2 / 6 * 100, 1)  # 24.0 + 71.99
    assert b["d3_7_pct"] == round(2 / 6 * 100, 1)  # 72.0 + 167.99
    assert b["over_7d_pct"] == round(1 / 6 * 100, 1)  # 168.0


def test_pct_sums_to_100() -> None:
    # При любом валидном распределении сумма pct ≈ 100 (rounding tolerance)
    res = _bucketize_settle_hours([1.0, 25.0, 30.0, 75.0, 80.0, 200.0, 300.0])
    b = res["buckets"]
    total = b["under_24h_pct"] + b["d1_3_pct"] + b["d3_7_pct"] + b["over_7d_pct"]
    assert abs(total - 100.0) < 0.5  # rounding до 1 decimal


def test_median_odd_n() -> None:
    res = _bucketize_settle_hours([10.0, 20.0, 30.0, 40.0, 50.0])
    assert res["median_hours"] == 30.0


def test_all_over_7d() -> None:
    # Long-horizon detector сценарий (D5)
    res = _bucketize_settle_hours([200.0, 300.0, 400.0, 500.0, 600.0])
    b = res["buckets"]
    assert b["over_7d_pct"] == 100.0
    assert b["under_24h_pct"] == 0.0
    assert res["median_hours"] == 400.0
