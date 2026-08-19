"""CLI: reconciliation backtest→shadow→virtual per-detector (гипотеза H3).

Собирает три источника правды per-detector на одну строку и считает divergence
(backtest-honest vs shadow), плюс implied haircut для пересчёта ручного.
Pure-расчёт — в `app/services/detector_stage_reconciliation.py` (тестируем без БД).

ДИАГНОСТИКА, НЕ авто-применение: haircut в docs/backtests НЕ переписывается.
Это рекомендация для человека (см. границы H3 в plan/report).

Запуск на проде (read-only SELECT, без side-effects):
    docker exec oraclepoly-backend python scripts/detector_stage_reconciliation.py \
        --detectors STANDALONE_V2_LGB,D36_obi_pressure,D41_momentum,D3_cross_mkt_arb,D38_resolution_lag

Без --detectors берёт дефолтный live-набор (5 детекторов N>=30 из H0).
--out пишет Markdown-таблицу в файл (паттерн прод-операционки: /tmp/X.md внутри
контейнера + cat), иначе печатает в stdout.
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from pathlib import Path

# Allow execution from repo root: PYTHONPATH=.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.services.database import close_db, init_db  # noqa: E402
from app.services.detector_stage_reconciliation import (  # noqa: E402
    ReconcileRow,
    build_sources,
    reconcile_detectors,
)

# Дефолт = live-набор N>=30 из H0 liveness (см. plan journal 2026-05-29).
DEFAULT_DETECTORS: tuple[str, ...] = (
    "STANDALONE_V2_LGB",
    "D36_obi_pressure",
    "D3_cross_mkt_arb",
    "D38_resolution_lag",
)
DEFAULT_DOCS_DIR = Path(__file__).resolve().parent.parent / "docs" / "backtests"


def _render_markdown(rows: list[ReconcileRow]) -> str:
    """Строки сверки → Markdown-таблица для отчёта/stdout."""
    lines = [
        "# Detector stage reconciliation (backtest → shadow → virtual)",
        "",
        "Divergence = backtest_honest_roi% − shadow_roi%. >0 → backtest оптимистичнее.",
        "implied_haircut = shadow / backtest_gross (какой haircut выровнял бы проекцию).",
        "ДИАГНОСТИКА, не авто-применение.",
        "",
        "| detector | status | backtest_honest% | shadow% | virtual_mean$ | N_settled | divergence_pp | implied_haircut |",
        "|---|---|---|---|---|---|---|---|",
    ]
    for r in rows:
        lines.append(
            f"| {r.detector} | {r.status} | {_fmt(r.backtest_honest_roi_pct)} | "
            f"{_fmt(r.shadow_roi_pct)} | {_fmt(r.virtual_mean_pnl_usd)} | {r.virtual_n_settled} | "
            f"{_fmt(r.backtest_vs_shadow_divergence_pct)} | {_fmt(r.implied_haircut_factor)} |"
        )
    return "\n".join(lines) + "\n"


def _fmt(v: float | None) -> str:
    return "—" if v is None else f"{v:.2f}"


async def _run(detectors: list[str], docs_dir: Path, out: Path | None) -> None:
    await init_db()
    try:
        sources = await build_sources(detectors, docs_dir)
    finally:
        await close_db()
    rows = reconcile_detectors(sources)
    md = _render_markdown(rows)
    if out is not None:
        out.write_text(md, encoding="utf-8")
        print(f"wrote {out}")
    else:
        print(md)


def main() -> None:
    parser = argparse.ArgumentParser(description="Detector stage reconciliation (H3)")
    parser.add_argument(
        "--detectors",
        type=str,
        default="",
        help="Comma-separated detector names. Default = live N>=30 set.",
    )
    parser.add_argument("--docs-dir", type=str, default=str(DEFAULT_DOCS_DIR))
    parser.add_argument("--out", type=str, default="", help="Write Markdown to file instead of stdout.")
    args = parser.parse_args()

    detectors = [d.strip() for d in args.detectors.split(",") if d.strip()] or list(DEFAULT_DETECTORS)
    out = Path(args.out) if args.out else None
    asyncio.run(_run(detectors, Path(args.docs_dir), out))


if __name__ == "__main__":
    main()
