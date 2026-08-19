"""Тесты парсера IPS (Phase 2 C2). Чистый слой — без моков БД."""

from pathlib import Path

import pytest

from app.analytics.ips import (
    INACTIVE_IPS_GATE,
    IpsGate,
    IpsParseError,
    IpsPolicy,
    evaluate_ips_gate,
    ips_version_hash,
    load_ips_file,
    parse_ips,
)

VALID = """
# comment
capital_usd: 1000
max_drawdown_pct: -0.20
liquidity_need_usd: 200
weekly_time_budget_hours: 5
forbidden_categories: Politics, Weather
forbidden_platforms: none
"""


def test_parse_valid_full():
    p = parse_ips(VALID)
    assert p.capital_usd == 1000.0
    assert p.max_drawdown_pct == -0.20
    assert p.forbidden_categories == frozenset({"politics", "weather"})
    assert p.forbidden_platforms == frozenset()
    assert len(p.version_hash) == 16


def test_hash_ignores_comments_and_blank_lines():
    noisy = "# x\n\n" + VALID + "\n# y\n"
    assert ips_version_hash(noisy) == ips_version_hash(VALID)


def test_hash_changes_on_value_change():
    changed = VALID.replace("-0.20", "-0.25")
    assert ips_version_hash(changed) != ips_version_hash(VALID)


def test_unknown_key_rejected():
    with pytest.raises(IpsParseError, match="unknown key"):
        parse_ips(VALID + "\nextra_key: 1\n")


def test_missing_required_key_rejected():
    broken = VALID.replace("capital_usd: 1000", "")
    with pytest.raises(IpsParseError, match="capital_usd"):
        parse_ips(broken)


def test_unfilled_template_rejected():
    with pytest.raises(IpsParseError, match="не заполнен"):
        parse_ips(VALID.replace("1000", "<fill>"))


def test_prose_line_rejected():
    with pytest.raises(IpsParseError, match="line"):
        parse_ips(VALID + "\nэто проза без двоеточия\n")


def test_drawdown_must_be_negative_fraction():
    with pytest.raises(IpsParseError, match="max_drawdown_pct"):
        parse_ips(VALID.replace("-0.20", "0.20"))
    with pytest.raises(IpsParseError, match="max_drawdown_pct"):
        parse_ips(VALID.replace("-0.20", "-1.5"))


def test_load_ips_file_missing_returns_none(tmp_path: Path):
    assert load_ips_file(tmp_path / "nope.md") is None


def test_load_ips_file_parses(tmp_path: Path):
    f = tmp_path / "owner_ips.local.md"
    f.write_text(VALID, encoding="utf-8")
    p = load_ips_file(f)
    assert isinstance(p, IpsPolicy)


def test_load_ips_file_propagates_parse_error(tmp_path: Path):
    f = tmp_path / "owner_ips.local.md"
    f.write_text("garbage\n", encoding="utf-8")
    with pytest.raises(IpsParseError):
        load_ips_file(f)


def _policy(**over) -> IpsPolicy:
    base = dict(
        capital_usd=1000.0,
        max_drawdown_pct=-0.20,
        liquidity_need_usd=200.0,
        weekly_time_budget_hours=5.0,
        forbidden_categories=frozenset({"politics"}),
        forbidden_platforms=frozenset(),
        version_hash="abc123",
    )
    base.update(over)
    return IpsPolicy(**base)


def test_gate_clean():
    g = evaluate_ips_gate(_policy(), forbidden_live_exposure={}, portfolio_roi=-0.05)
    assert g == IpsGate(active=True, violated=False, violations=(), version_hash="abc123")


def test_gate_forbidden_category_exposure_violates():
    g = evaluate_ips_gate(_policy(), forbidden_live_exposure={"politics": 12.5}, portfolio_roi=0.01)
    assert g.violated
    assert any("politics" in v for v in g.violations)


def test_gate_non_forbidden_exposure_ignored():
    g = evaluate_ips_gate(_policy(), forbidden_live_exposure={"sports": 50.0}, portfolio_roi=0.0)
    assert not g.violated


def test_gate_portfolio_drawdown_violates():
    g = evaluate_ips_gate(_policy(), forbidden_live_exposure={}, portfolio_roi=-0.25)
    assert g.violated
    assert any("drawdown" in v for v in g.violations)


def test_gate_drawdown_boundary_strict_less_than():
    # Точная граница НЕ нарушает (strict <, симметрично порогам C1 — d20dacd6)
    g = evaluate_ips_gate(_policy(), forbidden_live_exposure={}, portfolio_roi=-0.20)
    assert not g.violated


def test_gate_no_live_history_is_clean():
    g = evaluate_ips_gate(_policy(), forbidden_live_exposure={}, portfolio_roi=None)
    assert not g.violated


def test_inactive_gate_singleton():
    assert INACTIVE_IPS_GATE == IpsGate(active=False, violated=False, violations=(), version_hash=None)
