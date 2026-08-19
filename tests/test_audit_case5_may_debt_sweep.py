"""Тесты чистых функций Phase 0 audit-скрипта replay-кейса 5
(scripts/research/audit_case5_may_debt_sweep.py).

Покрывает `parse_date_from_filename` (извлечение даты из имени файла/директории
research/, полный и месяц-только форматы), `is_before_cutoff` (водораздел
2026-06-13), `classify_debt_signals` (self-check эвристика признаков а/б/в на
реальных сниппетах из research/ — НЕ источник находок DEBT_REGISTRY, см.
docstring скрипта, но должна в принципе улавливать три признака брифа),
`_config_hash`/`_dataset_hash` (воспроизводимость без LLM) и `scan`
(применение курируемого реестра, включая always_include meta-finding).

Функции, требующие БД (`run` с dry_run=False — INSERT в agent_runs/agent_claims),
намеренно не покрыты здесь — БД недоступна локально (agent_database_url не
настроен, см. CLAUDE.md "БД: только прод через SSH"). `run(dry_run=True)`
косвенно проверяется через `scan()`/`_build_verdict()`, которые она вызывает.
"""

from __future__ import annotations

from datetime import date

from scripts.research.audit_case5_may_debt_sweep import (
    CUTOFF_DATE,
    DEBT_REGISTRY,
    DebtItem,
    _build_verdict,
    _config_hash,
    _dataset_hash,
    classify_debt_signals,
    is_before_cutoff,
    parse_date_from_filename,
    scan,
)

# --- parse_date_from_filename ---------------------------------------------------


def test_parse_date_full_date_md_file():
    assert parse_date_from_filename("market-calibration-baseline-2026-06-13.md") == date(2026, 6, 13)


def test_parse_date_full_date_directory_with_trailing_slash():
    assert parse_date_from_filename("edge-synthesis-2026-05-24/") == date(2026, 5, 24)


def test_parse_date_full_date_directory_no_trailing_slash():
    assert parse_date_from_filename("detector-rca-2026-05-26") == date(2026, 5, 26)


def test_parse_date_month_only_returns_last_day_of_month():
    """Месяц-только формат (напр. `detector-audit-2026-05`) — консервативно
    берём последний день месяца, чтобы не занижать 'возраст' находки."""
    assert parse_date_from_filename("detector-audit-2026-05") == date(2026, 5, 31)


def test_parse_date_month_only_february():
    assert parse_date_from_filename("some-report-2026-02") == date(2026, 2, 28)


def test_parse_date_no_date_returns_none():
    assert parse_date_from_filename("copy-trading-prediction-markets-whale-signals") is None


def test_parse_date_invalid_month_returns_none():
    assert parse_date_from_filename("bogus-2026-13") is None


def test_parse_date_invalid_day_returns_none():
    assert parse_date_from_filename("bogus-2026-02-30.md") is None


# --- is_before_cutoff -----------------------------------------------------------


def test_is_before_cutoff_true_for_may():
    assert is_before_cutoff(date(2026, 5, 24)) is True


def test_is_before_cutoff_false_for_cutoff_day_itself():
    """Cutoff-день сам НЕ 'строго раньше' — граничные документы (напр.
    whale-cluster-eda-2026-06-13) разбираются индивидуально в реестре, не
    автоматически этой функцией (см. scan() OR-условие == CUTOFF_DATE)."""
    assert is_before_cutoff(CUTOFF_DATE) is False


def test_is_before_cutoff_false_for_july():
    assert is_before_cutoff(date(2026, 7, 9)) is False


# --- classify_debt_signals (self-check эвристика, не источник находок) ----------


def test_classify_detects_win_rate_without_net_roi_mitigation():
    """Реальный сниппет detector-audit-2026-05/report.md:39 — WR_lo порог, нет net roi упоминания рядом."""
    text = "wr_lo < 0.40 and roi < -10: CUT — low WR + heavy negative ROI"
    assert "a" in classify_debt_signals(text)


def test_classify_does_not_flag_a_when_net_roi_explicit():
    text = "honest realized Net ROI net-of-fees per detector, hold-to-resolution"
    assert "a" not in classify_debt_signals(text)


def test_classify_flags_b_when_no_control_mentioned():
    text = "Wilson lower bound 0.89 on 178 signals, verdict KEEPER"
    assert "b" in classify_debt_signals(text)


def test_classify_does_not_flag_b_when_control_group_present():
    text = "compared against control group baseline, edge holds after adjustment"
    assert "b" not in classify_debt_signals(text)


def test_classify_flags_c_for_naive_eda_without_lookahead_guard():
    """Реальный сниппет whale-cluster-eda до пересчёта: naive EDA, walk-forward отсутствует."""
    text = "naive EDA took all tx for the market at once, no walk-forward split applied"
    assert "c" in classify_debt_signals(text)


def test_classify_does_not_flag_c_when_look_ahead_safe_explicit():
    """Реальная фраза из whale-cluster-eda-2026-06-13.md после пересчёта."""
    text = "look-ahead-safe пересчёт: кластер обязан завершиться >= 1 дня до end_date"
    assert "c" not in classify_debt_signals(text)


def test_classify_kronos_readme_snippet_flags_a():
    """Реальный сниппет research/kronos-crypto-strike-2026-05/README.md:92."""
    text = "No fees applied. Win rate 32% means we're correctly identifying mispricings ~1/3 of the time"
    assert "a" in classify_debt_signals(text)


# --- _config_hash / _dataset_hash -----------------------------------------------


def test_config_hash_deterministic_for_same_input():
    h1 = _config_hash(CUTOFF_DATE, DEBT_REGISTRY)
    h2 = _config_hash(CUTOFF_DATE, DEBT_REGISTRY)
    assert h1 == h2


def test_config_hash_differs_for_different_cutoff():
    h1 = _config_hash(date(2026, 6, 13), DEBT_REGISTRY)
    h2 = _config_hash(date(2026, 6, 15), DEBT_REGISTRY)
    assert h1 != h2


def test_config_hash_order_independent():
    """sorted() внутри _config_hash — порядок элементов в реестре не должен влиять на hash."""
    reversed_registry = tuple(reversed(DEBT_REGISTRY))
    assert _config_hash(CUTOFF_DATE, DEBT_REGISTRY) == _config_hash(CUTOFF_DATE, reversed_registry)


def test_dataset_hash_deterministic():
    assert _dataset_hash(50, 10) == _dataset_hash(50, 10)


def test_dataset_hash_differs_for_different_counts():
    assert _dataset_hash(50, 10) != _dataset_hash(51, 10)


# --- scan -------------------------------------------------------------------------


def test_scan_returns_deterministic_item_count():
    """Acceptance брифа: 'повторный прогон с тем же cutoff даёт тот же список файлов'."""
    result1 = scan()
    result2 = scan()
    assert len(result1.debt_items) == len(result2.debt_items)
    assert [i.source_path for i in result1.debt_items] == [i.source_path for i in result2.debt_items]


def test_scan_meets_minimum_acceptance_count():
    """Acceptance брифа: минимум 3-5 конкретных элементов долга."""
    result = scan()
    assert len(result.debt_items) >= 5


def test_scan_includes_both_closed_precedents():
    """Acceptance брифа: минимум 2 известных уже-закрытых прецедента (D44, drill-matrix)."""
    result = scan()
    closed = [i for i in result.debt_items if i.already_closed_precedent]
    assert len(closed) >= 2
    sources = {i.source_path for i in closed}
    assert any("edge-synthesis-2026-05-24" in s for s in sources)
    assert any("live-drill-tabletop" in s for s in sources)


def test_scan_all_items_have_explicit_quarantine_status():
    """Acceptance брифа: ни один элемент не остаётся молча 'активным как будто свежим'."""
    result = scan()
    for item in result.debt_items:
        assert item.quarantine_status in ("expired", "active")
        # 'active' допустим только с честным old last_verified_at = doc_date (не now()).
        assert item.doc_date < date(2026, 7, 20)


def test_scan_always_include_item_present_despite_being_after_cutoff():
    """MEMORY.md meta-finding (doc_date=2026-06-24, после cutoff) должен войти в
    результат благодаря always_include=True, а не быть отфильтрован по дате."""
    result = scan()
    meta_items = [i for i in result.debt_items if i.always_include]
    assert len(meta_items) == 1
    assert meta_items[0].doc_date > CUTOFF_DATE


def test_scan_excludes_hypothetical_item_after_cutoff_without_always_include():
    """Контрольный тест: обычный (не always_include) элемент датированный после
    cutoff должен быть отфильтрован — иначе scan() не соблюдает водораздел."""
    hypothetical = DebtItem(
        source_path="research/hypothetical-2026-07-01.md",
        doc_date=date(2026, 7, 1),
        debt_types=("a",),
        statement="hypothetical post-cutoff item for filter test",
        scope="policy:test",
        quarantine_status="expired",
        evidence_note="synthetic test fixture",
    )
    result = scan(registry=(*DEBT_REGISTRY, hypothetical))
    assert hypothetical not in result.debt_items


# --- _build_verdict -----------------------------------------------------------


def test_build_verdict_labels_as_inventory_not_verdict():
    """Open risk брифа: явно писать 'инвентаризация, не приговор'."""
    result = scan()
    verdict = _build_verdict(result)
    assert "ИНВЕНТАРИЗАЦИЯ" in verdict
    assert "не выносит окончательный вердикт" in verdict


def test_build_verdict_reports_expired_and_active_counts():
    result = scan()
    verdict = _build_verdict(result)
    n_expired = sum(1 for i in result.debt_items if i.quarantine_status == "expired")
    n_active = sum(1 for i in result.debt_items if i.quarantine_status == "active")
    assert f"{n_expired} -> status=expired" in verdict
    assert f"{n_active} -> status=active" in verdict
