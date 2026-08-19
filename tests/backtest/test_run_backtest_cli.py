"""Regression tests для CLI scripts/run_backtest.py.

Главный кейс: --out в несуществующий каталог должен СОЗДАТЬ каталог и
записать отчёт, а не падать FileNotFoundError ПОСЛЕ полного прогона
(минуты compute впустую). Регрессия на фикс 2026-06-14.
"""

from __future__ import annotations

import importlib.util
import sys
from datetime import UTC, datetime
from pathlib import Path

from app.backtest.schema import BacktestResult

_SCRIPT_PATH = Path(__file__).resolve().parents[2] / "scripts" / "run_backtest.py"


def _load_cli_module():
    """Импортирует scripts/run_backtest.py как модуль (не на sys.path по умолчанию)."""
    spec = importlib.util.spec_from_file_location("run_backtest_cli", _SCRIPT_PATH)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _fake_result() -> BacktestResult:
    """Минимальный валидный BacktestResult (n=0 ветка, без обращения к canonical)."""
    from app.backtest.schema import BacktestConfig

    cfg = BacktestConfig(
        detector_name="d44",
        start_date=datetime(2026, 1, 1, tzinfo=UTC),
        end_date=datetime(2026, 2, 1, tzinfo=UTC),
        canonical_path=Path("/mnt/data/canonical"),
    )
    return BacktestResult(
        config=cfg,
        n_signals_emitted=0,
        n_unique_markets=0,
        n_unique_events=0,
        n_wins=0,
        win_rate=0.0,
        wilson_lower_95=0.0,
        wilson_upper_95=0.0,
        avg_edge_pct=0.0,
        per_category={},
        top_losses=[],
        elapsed_sec=0.1,
        metadata={"reason": "test"},
    )


def test_out_creates_missing_parent_dir(tmp_path, monkeypatch):
    """--out в несуществующий вложенный каталог → каталог создаётся, файл пишется."""
    cli = _load_cli_module()
    # Мокаем тяжёлый прогон — canonical не нужен, тестируем только запись --out.
    monkeypatch.setattr(cli, "run_backtest", lambda config, use_trade_buckets=False: _fake_result())

    out_path = tmp_path / "deep" / "nested" / "missing" / "report.md"
    assert not out_path.parent.exists()

    argv = [
        "run_backtest.py",
        "--detector", "d44",
        "--start", "2026-01-01",
        "--end", "2026-02-01",
        "--out", str(out_path),
        "--quiet",
    ]
    monkeypatch.setattr(sys, "argv", argv)

    rc = cli.main()

    assert rc == 0
    assert out_path.exists(), "отчёт должен записаться в созданный каталог"
    content = out_path.read_text(encoding="utf-8")
    assert "Backtest report" in content
    assert "D44" in content


def test_out_writes_to_existing_dir(tmp_path, monkeypatch):
    """Контроль: запись в существующий каталог по-прежнему работает."""
    cli = _load_cli_module()
    monkeypatch.setattr(cli, "run_backtest", lambda config, use_trade_buckets=False: _fake_result())

    out_path = tmp_path / "report.md"
    argv = [
        "run_backtest.py",
        "--detector", "d44",
        "--start", "2026-01-01",
        "--end", "2026-02-01",
        "--out", str(out_path),
        "--quiet",
    ]
    monkeypatch.setattr(sys, "argv", argv)

    rc = cli.main()

    assert rc == 0
    assert out_path.exists()
