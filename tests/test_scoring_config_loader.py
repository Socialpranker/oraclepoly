"""Tests for scoring_config_loader."""

from unittest.mock import patch

from app.core.scoring_config_loader import _SETTINGS_TO_WEIGHT_MAP, load_prod_config
from app.models.feature_vector import ScoringConfig


def test_load_prod_config_returns_ScoringConfig():
    cfg = load_prod_config()
    assert isinstance(cfg, ScoringConfig)


def test_load_prod_config_label_is_prod():
    cfg = load_prod_config()
    assert cfg.label == "prod"


def test_load_prod_config_weights_match_all_settings():
    """Loop-assert every (settings_attr, weight_key) pair maps correctly."""
    with patch("app.core.scoring_config_loader.get_settings") as mock:
        settings_obj = mock.return_value
        for i, settings_attr in enumerate(_SETTINGS_TO_WEIGHT_MAP.keys()):
            setattr(settings_obj, settings_attr, float(i) * 0.1 + 0.01)

        cfg = load_prod_config()
        for settings_attr, weight_key in _SETTINGS_TO_WEIGHT_MAP.items():
            expected = getattr(settings_obj, settings_attr)
            assert cfg.weights[weight_key] == expected, f"mismatch {settings_attr}→{weight_key}"


def test_load_prod_config_maker_taker_defaults_unchanged():
    """w_maker_direction / w_taker_imbalance теперь из settings, но дефолты те же
    (0.5 / 0.0) — миграция из хардкода не меняет поведение."""
    cfg = load_prod_config()
    assert cfg.weights["w_maker_direction"] == 0.5
    assert cfg.weights["w_taker_imbalance"] == 0.0


def test_load_prod_config_formula_flags_and_calibration():
    cfg = load_prod_config()
    assert cfg.formula_flags == {"taker_mode": "contrarian"}
    assert cfg.calibration_source == "global"


def test_load_prod_config_hash_stable_under_same_inputs():
    """Two loads from the same mocked settings produce equal hashes."""
    with patch("app.core.scoring_config_loader.get_settings") as mock:
        for settings_attr in _SETTINGS_TO_WEIGHT_MAP.keys():
            setattr(mock.return_value, settings_attr, 0.5)
        h1 = load_prod_config().hash
        h2 = load_prod_config().hash
        assert h1 == h2


def test_load_prod_config_hash_differs_when_weight_changes():
    """Mutating one weight must change hash."""
    with patch("app.core.scoring_config_loader.get_settings") as mock:
        for settings_attr in _SETTINGS_TO_WEIGHT_MAP.keys():
            setattr(mock.return_value, settings_attr, 0.5)
        h_base = load_prod_config().hash

    with patch("app.core.scoring_config_loader.get_settings") as mock:
        for settings_attr in _SETTINGS_TO_WEIGHT_MAP.keys():
            setattr(mock.return_value, settings_attr, 0.5)
        mock.return_value.scoring_w_whale = 0.99
        h_mutated = load_prod_config().hash

    assert h_base != h_mutated
