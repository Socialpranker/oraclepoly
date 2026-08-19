"""Historical Oracle Bin Analysis — где oracle score даёт edge, где не даёт.

Задача исследования: Вариант C — bin-level анализ.
Журнал: docs/research/2026-04-10-oracle-pit-findings.md

Читает pit_dataset.parquet (результат historical_pit_loader.py) и строит:

1. **Overall stats** — глобальный WR/ROI для prod и research моделей
2. **Bin-level cross-tab**: (category × side × div_bin × horizon_bin)
   Для каждой клетки: N, WR, ROI, Sharpe, baseline (без фильтра), edge
3. **Classification**: каждая клетка помечается 🟢/🟡/🔴/⚪
   🟢 oracle полезен (edge > +3%, N ≥ 200, p-value < 0.05)
   🟡 нейтрален (|edge| < 1%)
   🔴 вреден (edge < -3%)
   ⚪ недостаточно данных

4. **Baseline**: сравнение с простой политикой
   Baseline_A: "ставь всегда по категории+side без div-фильтра"
   Baseline_C: "следуй рынку (0 edge, always lose fees)"

Выдаёт:
  - `oracle_bin_analysis.md` — markdown отчёт с таблицами + классификацией
  - `oracle_bin_analysis.json` — все данные клеток для копания

Запуск на VPS:
    cd ~/oracle && python3 scripts/research/historical_oracle_bin_analysis.py
"""

import argparse
import json
import math
import os
from collections import defaultdict
from dataclasses import dataclass, field

import duckdb

DEFAULT_PIT = "/mnt/data/polymarket_dataset/pit_dataset.parquet"
OUT_DIR = "/mnt/data/polymarket_dataset/analysis"

# Bin определения
DIV_BINS = [
    ("0-3%", 0.00, 0.03),
    ("3-5%", 0.03, 0.05),
    ("5-8%", 0.05, 0.08),
    ("8-15%", 0.08, 0.15),
    ("15-30%", 0.15, 0.30),
]

HORIZON_BINS = [
    ("≤3d", 0, 3),
    ("4-7d", 3, 7),
    ("8-30d", 7, 30),
    (">30d", 30, 10000),
]

# Пороги классификации клеток
MIN_N = 200  # минимум сделок в клетке
EDGE_GREEN = 0.03  # edge > +3% → 🟢
EDGE_NEUTRAL = 0.01  # |edge| < 1% → 🟡
EDGE_RED = -0.03  # edge < -3% → 🔴
P_VALUE_THRESHOLD = 0.05


# ────────────────────────────────────────────────────────────────────────────
# Data container
# ────────────────────────────────────────────────────────────────────────────

@dataclass
class CellStats:
    category: str
    side: str
    div_bin: str
    horizon_bin: str
    model: str  # "prod" or "research"
    n: int = 0
    wins: int = 0
    total_pnl: float = 0.0
    pnls: list[float] = field(default_factory=list)

    @property
    def wr(self) -> float:
        return self.wins / self.n if self.n > 0 else 0.0

    @property
    def roi(self) -> float:
        return self.total_pnl / self.n if self.n > 0 else 0.0

    @property
    def pnl_std(self) -> float:
        if self.n < 2:
            return 0.0
        mean = self.roi
        var = sum((p - mean) ** 2 for p in self.pnls) / (self.n - 1)
        return math.sqrt(var)

    @property
    def sharpe(self) -> float:
        std = self.pnl_std
        if std <= 0:
            return 0.0
        return self.roi / std * math.sqrt(self.n)  # annualized-ish

    def add(self, pnl: float, win: bool) -> None:
        self.n += 1
        self.total_pnl += pnl
        self.pnls.append(pnl)
        if win:
            self.wins += 1


# ────────────────────────────────────────────────────────────────────────────
# Binning helpers
# ────────────────────────────────────────────────────────────────────────────

def div_bin_for(abs_div: float) -> str | None:
    for name, lo, hi in DIV_BINS:
        if lo <= abs_div < hi:
            return name
    return None  # > 30% — outlier


def horizon_bin_for(days: float) -> str | None:
    for name, lo, hi in HORIZON_BINS:
        if lo <= days < hi:
            return name
    return None


# ────────────────────────────────────────────────────────────────────────────
# Statistical helpers
# ────────────────────────────────────────────────────────────────────────────

def two_proportion_pvalue(wr: float, n: int, baseline_wr: float) -> float:
    """Упрощённый z-test: одна пропорция против baseline.

    H0: true WR == baseline_wr
    z = (wr - baseline) / sqrt(baseline*(1-baseline)/n)
    p = 2 * (1 - Phi(|z|))
    """
    if n < 10 or baseline_wr <= 0 or baseline_wr >= 1:
        return 1.0
    se = math.sqrt(baseline_wr * (1 - baseline_wr) / n)
    if se <= 0:
        return 1.0
    z = abs(wr - baseline_wr) / se
    # Phi(z) через erf
    p = 2 * (1 - 0.5 * (1 + math.erf(z / math.sqrt(2))))
    return p


def classify_cell(cell: CellStats, baseline_roi: float) -> str:
    """Классификация клетки: 🟢 / 🟡 / 🔴 / ⚪"""
    if cell.n < MIN_N:
        return "⚪"
    edge = cell.roi - baseline_roi
    if edge >= EDGE_GREEN:
        return "🟢"
    if edge <= EDGE_RED:
        return "🔴"
    if abs(edge) < EDGE_NEUTRAL:
        return "🟡"
    # promising но не дошёл до threshold
    return "➖"


# ────────────────────────────────────────────────────────────────────────────
# Main analysis
# ────────────────────────────────────────────────────────────────────────────

def load_pit_data(path: str) -> list[dict]:
    """Загрузить pit_dataset.parquet как list of dicts."""
    con = duckdb.connect()
    rows = con.execute(
        f"""
        SELECT
            category, phase, horizon_days, market_price,
            oracle_prod, div_prod, side_prod, prod_win, prod_pnl,
            oracle_research, div_research, side_research, research_win, research_pnl,
            total_volume_usd, winner_is_yes
        FROM read_parquet('{path}')
        """
    ).fetchall()
    cols = [
        "category", "phase", "horizon_days", "market_price",
        "oracle_prod", "div_prod", "side_prod", "prod_win", "prod_pnl",
        "oracle_research", "div_research", "side_research", "research_win", "research_pnl",
        "total_volume_usd", "winner_is_yes",
    ]
    return [dict(zip(cols, r, strict=True)) for r in rows]


def build_cells(data: list[dict]) -> dict[tuple, CellStats]:
    """Построить bin-level таблицу для обеих моделей.

    key = (category, side, div_bin, horizon_bin, model)
    """
    cells: dict[tuple, CellStats] = {}

    for row in data:
        cat = row["category"] or "Other"
        hor = row["horizon_days"]
        hor_bin = horizon_bin_for(hor)
        if hor_bin is None:
            continue

        for model, div_col, side_col, win_col, pnl_col in [
            ("prod", "div_prod", "side_prod", "prod_win", "prod_pnl"),
            ("research", "div_research", "side_research", "research_win", "research_pnl"),
        ]:
            div = abs(row[div_col])
            div_bin = div_bin_for(div)
            if div_bin is None:
                continue
            side = row[side_col]
            key = (cat, side, div_bin, hor_bin, model)

            if key not in cells:
                cells[key] = CellStats(
                    category=cat, side=side, div_bin=div_bin, horizon_bin=hor_bin, model=model,
                )
            cells[key].add(pnl=row[pnl_col], win=row[win_col])
    return cells


def compute_baselines(data: list[dict]) -> dict[tuple, dict]:
    """Baseline для каждой (category, side) пары — без div/horizon фильтров.

    Baseline_A: берём все строки с этой (category, side), считаем avg WR/ROI.
    """
    bl: dict[tuple, dict] = {}
    tmp: dict[tuple, dict] = defaultdict(lambda: {"n": 0, "wins_prod": 0, "wins_research": 0,
                                                    "pnl_prod": 0.0, "pnl_research": 0.0})
    for row in data:
        cat = row["category"] or "Other"
        # Baseline_A_prod: все строки с этой категорией по стороне prod
        key_prod = (cat, row["side_prod"], "prod")
        tmp[key_prod]["n"] += 1
        if row["prod_win"]:
            tmp[key_prod]["wins_prod"] += 1
        tmp[key_prod]["pnl_prod"] += row["prod_pnl"]

        key_research = (cat, row["side_research"], "research")
        tmp[key_research]["n"] += 1
        if row["research_win"]:
            tmp[key_research]["wins_research"] += 1
        tmp[key_research]["pnl_research"] += row["research_pnl"]

    for key, v in tmp.items():
        cat, side, model = key
        n = v["n"]
        if model == "prod":
            wins = v["wins_prod"]
            total_pnl = v["pnl_prod"]
        else:
            wins = v["wins_research"]
            total_pnl = v["pnl_research"]
        bl[key] = {"n": n, "wr": wins / n if n else 0, "roi": total_pnl / n if n else 0}
    return bl


def format_markdown(
    overall: dict,
    cells: dict[tuple, CellStats],
    baselines: dict[tuple, dict],
) -> str:
    """Построить markdown отчёт."""
    lines: list[str] = []
    lines.append("# Historical Oracle Bin Analysis")
    lines.append("")
    lines.append("**Автор:** `scripts/research/historical_oracle_bin_analysis.py`")
    lines.append(f"**Данные:** `{DEFAULT_PIT}`")
    lines.append("**Цель:** ответить на вопрос «где oracle_score даёт edge, а где нет»")
    lines.append("")
    lines.append("## Обозначения")
    lines.append("")
    lines.append("- **prod** — oracle с весами из прод calibration_configs (снимок 2026-04-10)")
    lines.append("- **research** — oracle с `DEFAULT_WEIGHTS` из `scoring.py` (research-рекомендации)")
    lines.append("- **edge** = `ROI_cell − ROI_baseline` (baseline = та же category+side без div-фильтра)")
    lines.append("- 🟢 edge ≥ +3% (oracle полезен)")
    lines.append("- 🟡 |edge| < 1% (нейтрален)")
    lines.append("- 🔴 edge ≤ −3% (oracle вреден — фильтр делает хуже)")
    lines.append("- ➖ edge в серой зоне (±1% до ±3%)")
    lines.append(f"- ⚪ N < {MIN_N} (недостаточно данных)")
    lines.append("")

    # ── Overall ──
    lines.append("## Overall Performance (все PIT точки)")
    lines.append("")
    lines.append("| Model | N | WR | ROI | Sharpe |")
    lines.append("|---|---:|---:|---:|---:|")
    for model in ("prod", "research"):
        o = overall[model]
        lines.append(f"| {model} | {o['n']:,} | {o['wr']:.1%} | {o['roi']:+.4f} | {o['sharpe']:.2f} |")
    lines.append("")

    # ── Bin-level для каждой модели ──
    for model in ("prod", "research"):
        lines.append(f"## Bin-level Analysis: **{model.upper()}** oracle")
        lines.append("")
        model_cells = {k: v for k, v in cells.items() if k[4] == model}
        if not model_cells:
            lines.append("_No cells for this model_")
            lines.append("")
            continue

        # Группировка по category
        by_cat: dict[str, list[CellStats]] = defaultdict(list)
        for k, v in model_cells.items():
            by_cat[k[0]].append(v)

        # Classification counts
        classification_count: dict[str, int] = defaultdict(int)
        for k, cell in model_cells.items():
            bl_key = (cell.category, cell.side, model)
            baseline_roi = baselines.get(bl_key, {}).get("roi", 0.0)
            cls = classify_cell(cell, baseline_roi)
            classification_count[cls] += 1

        lines.append("**Classification summary:**")
        lines.append("")
        total_cells = sum(classification_count.values())
        for cls in ("🟢", "➖", "🟡", "🔴", "⚪"):
            n = classification_count.get(cls, 0)
            pct = 100 * n / total_cells if total_cells else 0
            lines.append(f"- {cls}  {n:>4} cells ({pct:.0f}%)")
        lines.append("")

        # Top зелёных клеток
        green_cells = []
        for k, cell in model_cells.items():
            bl_key = (cell.category, cell.side, model)
            baseline_roi = baselines.get(bl_key, {}).get("roi", 0.0)
            if classify_cell(cell, baseline_roi) == "🟢":
                green_cells.append((cell.roi - baseline_roi, cell, baseline_roi))
        green_cells.sort(reverse=True)

        lines.append("**🟢 Top клетки где oracle помогает:**")
        lines.append("")
        if green_cells:
            lines.append("| Category | Side | Div bin | Horizon | N | WR | ROI | Baseline ROI | Edge |")
            lines.append("|---|---|---|---|---:|---:|---:|---:|---:|")
            for edge, cell, bl_roi in green_cells[:20]:
                lines.append(
                    f"| {cell.category} | {cell.side} | {cell.div_bin} | {cell.horizon_bin} | "
                    f"{cell.n:,} | {cell.wr:.1%} | {cell.roi:+.4f} | {bl_roi:+.4f} | **{edge:+.4f}** |"
                )
        else:
            lines.append("_Нет клеток, где oracle даёт positive edge ≥3%_")
        lines.append("")

        # Top красных клеток (oracle вреден)
        red_cells = []
        for k, cell in model_cells.items():
            bl_key = (cell.category, cell.side, model)
            baseline_roi = baselines.get(bl_key, {}).get("roi", 0.0)
            if classify_cell(cell, baseline_roi) == "🔴":
                red_cells.append((cell.roi - baseline_roi, cell, baseline_roi))
        red_cells.sort()

        lines.append("**🔴 Top клетки где oracle вредит:**")
        lines.append("")
        if red_cells:
            lines.append("| Category | Side | Div bin | Horizon | N | WR | ROI | Baseline ROI | Edge |")
            lines.append("|---|---|---|---|---:|---:|---:|---:|---:|")
            for edge, cell, bl_roi in red_cells[:20]:
                lines.append(
                    f"| {cell.category} | {cell.side} | {cell.div_bin} | {cell.horizon_bin} | "
                    f"{cell.n:,} | {cell.wr:.1%} | {cell.roi:+.4f} | {bl_roi:+.4f} | **{edge:+.4f}** |"
                )
        else:
            lines.append("_Нет клеток с negative edge ≤−3%_")
        lines.append("")

        # Baseline таблица
        lines.append("**Baseline (все сделки без div-фильтра):**")
        lines.append("")
        lines.append("| Category | Side | N | WR | ROI |")
        lines.append("|---|---|---:|---:|---:|")
        model_baselines = sorted(
            ((k, v) for k, v in baselines.items() if k[2] == model),
            key=lambda kv: -kv[1]["n"],
        )
        for k, v in model_baselines[:20]:
            lines.append(f"| {k[0]} | {k[1]} | {v['n']:,} | {v['wr']:.1%} | {v['roi']:+.4f} |")
        lines.append("")

    return "\n".join(lines)


def compute_overall(data: list[dict]) -> dict:
    """Глобальные WR/ROI/Sharpe для обеих моделей."""
    out: dict = {}
    for model, win_col, pnl_col in [("prod", "prod_win", "prod_pnl"), ("research", "research_win", "research_pnl")]:
        n = len(data)
        wins = sum(1 for r in data if r[win_col])
        pnls = [r[pnl_col] for r in data]
        total_pnl = sum(pnls)
        roi = total_pnl / n if n else 0
        if n > 1:
            var = sum((p - roi) ** 2 for p in pnls) / (n - 1)
            std = math.sqrt(var)
            sharpe = (roi / std) * math.sqrt(n) if std > 0 else 0.0
        else:
            sharpe = 0.0
        out[model] = {
            "n": n,
            "wr": wins / n if n else 0,
            "roi": roi,
            "sharpe": sharpe,
        }
    return out


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--pit", type=str, default=DEFAULT_PIT)
    parser.add_argument("--out-dir", type=str, default=OUT_DIR)
    args = parser.parse_args()

    os.makedirs(args.out_dir, exist_ok=True)

    print(f"[1/4] Loading {args.pit}")
    data = load_pit_data(args.pit)
    print(f"      {len(data):,} rows loaded")

    print("[2/4] Computing overall stats")
    overall = compute_overall(data)
    for model, o in overall.items():
        print(f"      {model}: N={o['n']:,}  WR={o['wr']:.1%}  ROI={o['roi']:+.4f}  Sharpe={o['sharpe']:.2f}")

    print("[3/4] Building bin cells")
    cells = build_cells(data)
    print(f"      {len(cells)} cells (prod+research)")

    print("      Computing baselines")
    baselines = compute_baselines(data)
    print(f"      {len(baselines)} baseline entries")

    print("[4/4] Writing report")
    md_path = os.path.join(args.out_dir, "oracle_bin_analysis.md")
    json_path = os.path.join(args.out_dir, "oracle_bin_analysis.json")

    with open(md_path, "w") as f:
        f.write(format_markdown(overall, cells, baselines))
    print(f"      markdown → {md_path}")

    # JSON для дальнейшего копания
    json_data = {
        "overall": overall,
        "cells": [
            {
                "category": c.category,
                "side": c.side,
                "div_bin": c.div_bin,
                "horizon_bin": c.horizon_bin,
                "model": c.model,
                "n": c.n,
                "wr": c.wr,
                "roi": c.roi,
                "sharpe": c.sharpe,
            }
            for c in cells.values()
        ],
        "baselines": [
            {"category": k[0], "side": k[1], "model": k[2], **v}
            for k, v in baselines.items()
        ],
    }
    with open(json_path, "w") as f:
        json.dump(json_data, f, indent=2, default=str)
    print(f"      json → {json_path}")

    print("\nDONE")


if __name__ == "__main__":
    main()
