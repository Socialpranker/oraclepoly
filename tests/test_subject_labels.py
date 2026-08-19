# tests/test_subject_labels.py
from app.analytics.subject_labels import _DETECTOR_LABELS, humanize_subject

# Снимок detector_state на проде 2026-08-04. Обновлять вместе со словарём —
# он существует, чтобы разъезд с продом падал тестом, а не всплывал в TG.
PROD_DETECTORS_2026_08_04 = {
    "D13_retail_confidence_fade",
    "D14_crypto_winners_distribute_no",
    "D26_fade_whale_extreme",
    "D29_negrisk_basket_arb",
    "D36_obi_pressure",
    "D38_resolution_lag",
    "D3_cross_mkt_arb",
    "D40_kalshi_cross_platform",
    "D42_event_driven_ml",
    "D44_late_convergence_carry",
    "D45b_lead_lag_cross_venue",
    "D49_strike_ladder",
    "M2_liquidity_vacuum",
    "STANDALONE_V2_LGB",
    "d45_chainlink_leadlag",
}


def test_known_detector_gets_human_name():
    assert humanize_subject("detector:D38_resolution_lag") == "D38 «Задержка резолва»"


def test_unknown_detector_falls_back_to_raw_id():
    """fail-open: неизвестный детектор печатается как есть, джоба не падает."""
    assert humanize_subject("detector:D99_unknown") == "D99_unknown"


def test_non_detector_subject_returns_as_is():
    assert humanize_subject("proxy:iproyal") == "proxy:iproyal"


def test_empty_subject_does_not_crash():
    assert humanize_subject("") == ""


def test_every_prod_detector_has_human_name():
    """Каждый живой детектор обязан иметь имя. Пропуск = машинный id в сообщении владельцу."""
    missing = PROD_DETECTORS_2026_08_04 - _DETECTOR_LABELS.keys()
    assert not missing, f"нет человеческого имени: {sorted(missing)}"


def test_no_phantom_detectors_in_dictionary():
    """Словарь не содержит выдуманных id.

    Стережёт конкретную ошибку: первая версия словаря писалась по памяти и содержала
    4 несуществующих детектора, двое из которых отбирали номер у реальных (D44/D45).
    """
    phantom = _DETECTOR_LABELS.keys() - PROD_DETECTORS_2026_08_04
    assert not phantom, f"детекторов нет в проде: {sorted(phantom)}"
