"""V2 Execution — cal_p zone blacklist tests.

Покрывает _is_cal_p_zone_blacklisted — per-(detector, cal_p zone) reject-gate.

Эмпирика 2026-05-19 (30d virtual, settled orders):
  Зоны cal_p глобально:
    0.50-0.70:   ROI=-5.6%,  PnL=-$4,265 (главный живой: V2_LGB -$1,532)
    ≥0.85:      ROI=-1.5%,  PnL=-$850  (главный живой: D3 -$786)
  Прибыльные (keep): 0.15-0.30 (+117%), 0.30-0.50 (+18.7%), 0.70-0.85 (+12%)

Контракт _is_cal_p_zone_blacklisted(detector, cal_p):
  - Returns (is_blacklisted, zone_label).
  - cal_p None → (False, None) — защита от missing data.
  - detector не в blacklist → (False, None) для любого cal_p.
  - cal_p попадает в любой из intervals → (True, "lo-hi").
  - Intervals: lo_inclusive, hi_exclusive.
"""

from __future__ import annotations

from app.jobs.v2_execution import (
    _CAL_P_ZONE_BLACKLIST,
    _is_cal_p_zone_blacklisted,
)

# ---------- Per-detector zone blacklist ----------


def test_standalone_v2_lgb_midhigh_blacklisted():
    """STANDALONE_V2_LGB в midhigh 0.50-0.70 → reject. -$1,532 на n=66."""
    blocked, label = _is_cal_p_zone_blacklisted("STANDALONE_V2_LGB", 0.60)
    assert blocked is True
    assert label == "0.50-0.70"


def test_standalone_v2_lgb_low_zone_NOT_blocked():
    """STANDALONE_V2_LGB в [0.15-0.30] — +$1,447. НЕ блочим."""
    blocked, _ = _is_cal_p_zone_blacklisted("STANDALONE_V2_LGB", 0.20)
    assert blocked is False


def test_d3_cross_mkt_arb_high_zone_blacklisted():
    """D3 в зоне ≥0.85 → reject (-$786 на n=20)."""
    blocked, label = _is_cal_p_zone_blacklisted("D3_cross_mkt_arb", 0.90)
    assert blocked is True
    assert label == "0.85-1.01"


def test_d3_cross_mkt_arb_mid_high_NOT_blocked():
    """D3 в 0.70-0.85 — +$481. НЕ блочим (только ≥0.85)."""
    blocked, _ = _is_cal_p_zone_blacklisted("D3_cross_mkt_arb", 0.75)
    assert blocked is False


def test_standalone_v2_lgb_low_zone_NOT_blocked_below_band():
    """STANDALONE_V2_LGB в 0.30-0.50 (ниже blacklist-зоны) → НЕ блочим."""
    blocked, _ = _is_cal_p_zone_blacklisted("STANDALONE_V2_LGB", 0.40)
    assert blocked is False


# ---------- Defaults ----------


def test_unblacklisted_detector_any_zone_not_blocked():
    """Детектор не в blacklist — никогда не блокируется по cal_p."""
    blocked, label = _is_cal_p_zone_blacklisted("D14_crypto_winners", 0.55)
    assert blocked is False
    assert label is None


def test_none_cal_p_returns_false():
    """cal_p=None (cold-start) → не блокируем (другие gates сработают)."""
    blocked, _ = _is_cal_p_zone_blacklisted("D26_fade_whale_extreme", None)
    assert blocked is False


def test_unknown_detector_returns_false():
    """Несуществующий detector → не блокируем."""
    blocked, _ = _is_cal_p_zone_blacklisted("D99_does_not_exist", 0.12)
    assert blocked is False


# ---------- Boundary semantics: [lo, hi) ----------


def test_lower_boundary_inclusive():
    """cal_p == lo → blocked (inclusive). STANDALONE zone [0.50, 0.70)."""
    blocked, _ = _is_cal_p_zone_blacklisted("STANDALONE_V2_LGB", 0.50)
    assert blocked is True


def test_upper_boundary_exclusive():
    """cal_p == hi → NOT blocked (exclusive). 0.70 для STANDALONE → НЕ blocked."""
    blocked, _ = _is_cal_p_zone_blacklisted("STANDALONE_V2_LGB", 0.70)
    assert blocked is False


# ---------- Sanity checks на blacklist данных ----------


def test_all_blacklist_zones_have_valid_ranges():
    """lo < hi и оба в [0, 1.01] (1.01 = sentinel для ≥0.85 финальной зоны)."""
    for detector, zones in _CAL_P_ZONE_BLACKLIST.items():
        for lo, hi in zones:
            assert 0.0 <= lo < hi <= 1.01, f"Invalid zone for {detector}: [{lo}, {hi})"


def test_all_known_problematic_detectors_in_blacklist():
    """Живые детекторы с проблемными зонами (PnL <-$300 на n≥5) должны быть в blacklist."""
    expected_detectors = {
        "STANDALONE_V2_LGB",
        "D3_cross_mkt_arb",
    }
    assert expected_detectors.issubset(set(_CAL_P_ZONE_BLACKLIST.keys()))
