"""
Aggregation script для NegRisk arb scanner JSONL → стат-сводка.

Цель: после 24-72 часов накопления данных через `negrisk_arb_scanner --interval 1h`
посмотреть на распределение `realistic_edge_bps` по time-of-day, event_type,
event_age, time_to_resolve.

Decision rule (выбран до сбора данных):
  < 5%  сканов с edge > 50bps → закрыть направление
  5-15% → marginally interesting, в backlog
  > 15% → структурный паттерн, копать дальше

Запуск (на VPS):
    python -m scripts.research.negrisk_aggregate

Без зависимостей кроме stdlib. Если на VPS есть duckdb — можно ad-hoc query
через shell, но базовая сводка работает чистым Python для simplicity.
"""

import argparse
import json
import os
import statistics
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any

DEFAULT_DIR = Path(os.getenv("NEGRISK_OUT_DIR", "./research_out/negrisk_arb"))


def _load_rows(directory: Path) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for f in sorted(directory.glob("*.jsonl")):
        with f.open() as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    rows.append(json.loads(line))
                except json.JSONDecodeError:
                    continue
    return rows


def _summary(rows: list[dict]) -> dict:
    edges = [r.get("realistic_edge_bps") for r in rows]
    edges_with_value = [e for e in edges if e is not None]
    edges_positive = [e for e in edges_with_value if e > 0]
    return {
        "total_rows": len(rows),
        "validated": len(edges_with_value),  # CLOB validation done
        "with_edge": len(edges_positive),
        "edge_50bps_plus": sum(1 for e in edges_with_value if e > 50),
        "edge_100bps_plus": sum(1 for e in edges_with_value if e > 100),
        "edge_50bps_pct": (
            100.0 * sum(1 for e in edges_with_value if e > 50) / len(edges_with_value) if edges_with_value else 0
        ),
        "max_edge_bps": max(edges_with_value, default=0),
        "median_edge_bps": statistics.median(edges_positive) if edges_positive else 0,
    }


def _by_dimension(rows: list[dict], key: str) -> dict[Any, dict]:
    """Группировка edges по dimension key, считаем p50/p90/max."""
    by: dict[Any, list[float]] = defaultdict(list)
    for r in rows:
        edge = r.get("realistic_edge_bps")
        if edge is None:
            continue
        dim = r.get(key)
        if dim is None:
            continue
        by[dim].append(edge)
    out = {}
    for dim, vals in by.items():
        positive = [v for v in vals if v > 0]
        out[dim] = {
            "n_validated": len(vals),
            "n_arb_50bps": sum(1 for v in vals if v > 50),
            "pct_arb_50bps": 100.0 * sum(1 for v in vals if v > 50) / len(vals) if vals else 0,
            "max": max(vals, default=0),
            "median_positive": statistics.median(positive) if positive else 0,
        }
    return out


def _bucket_age(days: float | None) -> str:
    if days is None:
        return "unknown"
    if days < 1:
        return "fresh_<1d"
    if days < 7:
        return "young_1-7d"
    if days < 30:
        return "mature_7-30d"
    return "old_>30d"


def _bucket_resolve(hours: float | None) -> str:
    if hours is None:
        return "unknown"
    if hours < 6:
        return "resolving_<6h"
    if hours < 24:
        return "imminent_6-24h"
    if hours < 168:
        return "near_1-7d"
    return "far_>7d"


def _enriched_rows(rows: list[dict]) -> list[dict]:
    for r in rows:
        r["age_bucket"] = _bucket_age(r.get("event_age_days"))
        r["resolve_bucket"] = _bucket_resolve(r.get("time_to_resolve_h"))
    return rows


def _print_table(title: str, by: dict[Any, dict]) -> None:
    print(f"\n=== {title} ===")
    if not by:
        print("  (no data)")
        return
    rows_sorted = sorted(by.items(), key=lambda kv: kv[1]["pct_arb_50bps"], reverse=True)
    print(f"  {'dim':25s} {'n_val':>6s} {'arb_50':>7s} {'pct':>6s}  {'max':>7s} {'med+':>7s}")
    for dim, st in rows_sorted:
        print(
            f"  {str(dim)[:25]:25s} {st['n_validated']:6d} {st['n_arb_50bps']:7d} "
            f"{st['pct_arb_50bps']:5.1f}% {st['max']:7.1f} {st['median_positive']:7.1f}"
        )


def _decision(summary: dict) -> str:
    pct = summary["edge_50bps_pct"]
    if pct < 5:
        return f"❌ ЗАКРЫТЬ направление (edge>50bps в {pct:.1f}% сканов < 5%)"
    if pct < 15:
        return f"🟡 MARGINAL — в backlog ({pct:.1f}% сканов с edge>50bps, 5-15%)"
    return f"🟢 СТРУКТУРНЫЙ ПАТТЕРН — копать дальше ({pct:.1f}% сканов >15%)"


def main() -> None:
    p = argparse.ArgumentParser()
    p.add_argument("--dir", type=Path, default=DEFAULT_DIR)
    args = p.parse_args()

    rows = _load_rows(args.dir)
    if not rows:
        print(f"No data in {args.dir}")
        return

    rows = _enriched_rows(rows)

    summary = _summary(rows)
    print(f"Loaded {summary['total_rows']} rows from {args.dir}")
    print(f"  Validated through CLOB: {summary['validated']}")
    print(f"  With edge > 0:          {summary['with_edge']}")
    print(f"  Edge > 50 bps:          {summary['edge_50bps_plus']} ({summary['edge_50bps_pct']:.1f}%)")
    print(f"  Edge > 100 bps:         {summary['edge_100bps_plus']}")
    print(f"  Max edge:               {summary['max_edge_bps']:.1f} bps")
    print(f"  Median positive edge:   {summary['median_edge_bps']:.1f} bps")

    print(f"\n{_decision(summary)}")

    _print_table("by hour_utc", _by_dimension(rows, "hour_utc"))
    _print_table("by event_type", _by_dimension(rows, "event_type"))
    _print_table("by age_bucket", _by_dimension(rows, "age_bucket"))
    _print_table("by resolve_bucket", _by_dimension(rows, "resolve_bucket"))

    # Top events with arbs
    arb_rows = [r for r in rows if (r.get("realistic_edge_bps") or 0) > 50]
    if arb_rows:
        print(f"\n=== Top arb events (n={len(arb_rows)}) ===")
        arb_rows.sort(key=lambda r: r.get("realistic_edge_bps", 0), reverse=True)
        seen_slugs: Counter = Counter()
        for r in arb_rows[:20]:
            slug = r.get("slug", "?")
            seen_slugs[slug] += 1
            print(
                f"  {r['ts'][:16]} {slug[:40]:40s} edge={r['realistic_edge_bps']:.1f}bps "
                f"type={r.get('event_type', '?')} note={r.get('note', '')[:40]}"
            )
        if len(arb_rows) > 20:
            print(f"  ... +{len(arb_rows) - 20} more")
        print("\nRecurring arb slugs:")
        for slug, n in seen_slugs.most_common(5):
            print(f"  {slug:50s}  {n}× hits")


if __name__ == "__main__":
    main()
