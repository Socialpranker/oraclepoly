"""Тесты SSoT-хелперов активности детекторов в detector_metrics."""

from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest


def test_parse_active_cron_ignores_comments(tmp_path):
    """Закомментированные cron-строки игнорируются, активные — парсятся."""
    from app.services.detector_metrics import _CRON_NAME_TO_DETECTOR, parse_active_cron_detectors

    sample_cron_name = next(iter(_CRON_NAME_TO_DETECTOR.keys()))
    sample_detector = _CRON_NAME_TO_DETECTOR[sample_cron_name]
    crontab = tmp_path / "crontab.conf"
    crontab.write_text(
        f"* * * * * cd /app && python cron_runner.py {sample_cron_name}\n"
        f"# * * * * * cd /app && python cron_runner.py {sample_cron_name}_disabled\n"
    )
    active = parse_active_cron_detectors(crontab)
    assert sample_detector in active


def test_parse_active_cron_missing_file_fail_open(tmp_path):
    """Нет crontab → fail-open (все известные детекторы), не пустой set."""
    from app.services.detector_metrics import _CRON_NAME_TO_DETECTOR, parse_active_cron_detectors

    active = parse_active_cron_detectors(tmp_path / "nonexistent.conf")
    assert active == set(_CRON_NAME_TO_DETECTOR.values())


def test_every_active_cron_scan_job_is_mapped():
    """Каждая активная dXX-scan строка в committed crontab.conf имеет запись в маппинге.

    Регресс на пропуск D49 (заведён 2026-07-10, обнаружен 2026-08-04): cron шёл
    каждые 5 минут, детектор эмитил ~144 сигнала/неделю, но записи в
    _CRON_NAME_TO_DETECTOR не было → он выпадал из `db_active & cron_active` и
    месяц не проверялся ни edge_decay_monitor (Wilson/Sortino/CUSUM), ни SLO.

    Прежние тесты этого не ловили: они брали сам маппинг за эталон
    (`active == set(_CRON_NAME_TO_DETECTOR.values())`), поэтому отсутствие
    детектора в маппинге для них не отличалось от его отсутствия в системе.
    """
    import re
    from pathlib import Path

    from app.services.detector_metrics import _CRON_NAME_TO_DETECTOR

    crontab = Path(__file__).resolve().parents[1] / "crontab.conf"
    assert crontab.exists(), f"committed crontab.conf не найден: {crontab}"

    # Только активные (не закомментированные) строки, только scan-джобы детекторов.
    active_scan_jobs = {
        m.group(1)
        for line in crontab.read_text().splitlines()
        if line.strip() and not line.lstrip().startswith("#")
        for m in [re.search(r"cron_runner\.py\s+(d\d+[a-z]?-scan)\b", line)]
        if m
    }
    assert active_scan_jobs, "в crontab.conf не найдено ни одной активной dXX-scan строки"

    unmapped = sorted(active_scan_jobs - set(_CRON_NAME_TO_DETECTOR))
    assert not unmapped, (
        f"активные cron-джобы без записи в _CRON_NAME_TO_DETECTOR: {unmapped}. "
        "Такой детектор невидим для edge_decay_monitor и detector_slo_monitor — "
        "добавь его в маппинг (app/services/detector_metrics.py)."
    )


def test_mapping_agrees_with_cron_runner_registry():
    """Маппинг и реестр cron_runner указывают на ОДИН детектор для каждого cron-имени.

    Регресс на противоречие d45-scan (обнаружено 2026-08-04): реестр вёл
    "d45-scan" на app.jobs.d45_lead_lag_scan (импортирует d45_lead_lag_cross_venue
    = D45b), а маппинг утверждал "d45_chainlink_leadlag" — другой детектор.
    Не стреляло только потому, что cron-строка была закомментирована; при
    расконсервации Kalshi мониторинг записал бы сигналы D45b на чужое имя.

    Сверяем машинно: cron-имя → модуль джобы (реестр) → какой модуль детектора
    та джоба импортирует → его DETECTOR_NAME. Должно совпасть с маппингом.
    """
    import re
    from pathlib import Path

    from app.services.detector_metrics import _CRON_NAME_TO_DETECTOR

    root = Path(__file__).resolve().parents[1]
    registry = dict(
        re.findall(
            r'"([a-z0-9_-]+-scan)":\s*\("app\.jobs\.([a-z0-9_]+)"',
            (root / "scripts/cron_runner.py").read_text(),
        )
    )

    mismatches = []
    for cron_name, expected_detector in _CRON_NAME_TO_DETECTOR.items():
        job_module = registry.get(cron_name)
        if job_module is None:
            continue  # покрыто отдельным тестом на фантомные ключи
        job_src = (root / "app/jobs" / f"{job_module}.py").read_text()
        # Какой модуль детектора импортирует джоба.
        imported = re.findall(r"from app\.detectors import (\w+)", job_src)
        names = set()
        for mod in imported:
            det_file = root / "app/detectors" / f"{mod}.py"
            if not det_file.exists():
                continue
            m = re.search(r'^DETECTOR_NAME[^=]*=\s*"([^"]+)"', det_file.read_text(), re.M)
            if m:
                names.add(m.group(1))
        if names and expected_detector not in names:
            mismatches.append(
                f"{cron_name}: маппинг говорит {expected_detector!r}, а app.jobs.{job_module} эмитит {sorted(names)}"
            )

    assert not mismatches, "маппинг противоречит реестру cron_runner:\n" + "\n".join(mismatches)


def test_mapping_has_no_phantom_cron_names():
    """Каждый ключ маппинга существует в реестре cron_runner — иначе он фантом.

    Регресс на "d45b-scan" (удалён 2026-08-04): ключ был в маппинге, но такого
    cron-имени нет ни в реестре, ни в crontab.conf — запустить по нему ничего
    нельзя, а присутствие создавало иллюзию, что D45b управляется через cron.
    """
    import re
    from pathlib import Path

    from app.services.detector_metrics import _CRON_NAME_TO_DETECTOR

    root = Path(__file__).resolve().parents[1]
    registry_names = set(
        re.findall(
            r'"([a-z0-9_-]+-scan)":\s*\("app\.jobs\.',
            (root / "scripts/cron_runner.py").read_text(),
        )
    )
    phantom = sorted(set(_CRON_NAME_TO_DETECTOR) - registry_names)
    assert not phantom, (
        f"ключи маппинга без cron-джобы в реестре: {phantom}. "
        "Либо зарегистрируй джобу в scripts/cron_runner.py, либо убери ключ — "
        "детектор без cron управляется иначе (WSS-хук или loop в main.py)."
    )


@pytest.mark.asyncio
async def test_fetch_active_detectors_intersects_db_and_cron():
    """fetch_active = db_active ∩ cron_active."""
    from app.services import detector_metrics

    db_rows = [{"detector": "D_in_both"}, {"detector": "D_db_only"}]
    with (
        patch.object(detector_metrics, "fetch", new=AsyncMock(return_value=db_rows)),
        patch.object(
            detector_metrics,
            "parse_active_cron_detectors",
            return_value={"D_in_both", "D_not_in_db"},
        ),
    ):
        result = await detector_metrics.fetch_active_detectors()
    assert result == {"D_in_both"}


@pytest.mark.asyncio
async def test_wss_realtime_detector_active_without_cron():
    """M2 (WSS-realtime) активен по БД даже без cron-строки (whitelist Вариант B)."""
    from app.services import detector_metrics

    db_rows = [{"detector": "M2_liquidity_vacuum"}, {"detector": "D_cron_only"}]
    with (
        patch.object(detector_metrics, "fetch", new=AsyncMock(return_value=db_rows)),
        patch.object(
            detector_metrics,
            "parse_active_cron_detectors",
            return_value={"D_cron_only"},  # M2 НЕ в cron
        ),
    ):
        result = await detector_metrics.fetch_active_detectors()
    assert "M2_liquidity_vacuum" in result  # whitelist спасает
    assert "D_cron_only" in result  # обычный db∩cron путь
