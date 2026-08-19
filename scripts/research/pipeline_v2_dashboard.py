"""CLI dashboard для решения о cutover на v2.

Stage 5 / Two-Stage ML Pipeline.

Читает свежий comparison report и печатает структурированный summary:
- Сколько накоплено (n_events, n_rows)
- Все gate'ы прошли?
- Per-category breakdown с алертами
- Recommendation: PROCEED / WAIT / BLOCK

Запускать перед HUMAN cutover decision:
    docker exec -i oraclepoly-backend python scripts/research/pipeline_v2_dashboard.py
"""

from __future__ import annotations

import asyncio
import sys

sys.path.insert(0, "/app")
sys.path.insert(0, ".")

from app.jobs.pipeline_v2_comparison_report import run_pipeline_v2_comparison_report  # noqa: E402
from app.services.database import close_db, init_db  # noqa: E402


async def run() -> None:
    await init_db(min_size=1, max_size=2)
    try:
        summary = await run_pipeline_v2_comparison_report()
    finally:
        await close_db()

    if summary.get("status") in ("no_data",):
        print("⚠️  No resolved shadow predictions yet. Wait until cron accumulates data.")
        return

    print("\n" + "=" * 60)
    print(f"  PIPELINE V2 COMPARISON DASHBOARD ({summary['as_of']})")
    print("=" * 60)

    print("\n📊 Sample size:")
    print(f"   n_rows:   {summary['n_rows']:,}")
    print(f"   n_events: {summary['n_events']:,}")

    print("\n📈 Per-event Brier (lower = better):")
    print(f"   monolith: {summary['mono_brier']:.4f}")
    print(f"   v2:       {summary['v2_brier']:.4f}")
    delta = summary["mono_brier"] - summary["v2_brier"]
    print(f"   delta:    {delta:+.4f} ({'v2 better' if delta > 0 else 'mono better'})")

    print("\n🎯 Top-1 per-event accuracy (higher = better):")
    print(f"   monolith: {summary['mono_top1_per_event']:.4f}")
    print(f"   v2:       {summary['v2_top1_per_event']:.4f}")

    print("\n📋 Per-category breakdown:")
    print(
        f"   {'category':<20} {'n_ev':>6} {'mono_b':>10} {'v2_b':>10} {'delta':>10} {'flag':>10}"
    )
    for cat, m in sorted(summary["per_category"].items(), key=lambda x: -x[1]["n_rows"]):
        d = m["mono_brier"] - m["v2_brier"]
        flag = ""
        if m["n_events"] < 10:
            flag = "tiny"
        elif d < -0.01:
            flag = "⚠️ worse"
        elif d > 0.01:
            flag = "✅ better"
        print(
            f"   {cat:<20} {m['n_events']:>6} {m['mono_brier']:>10.4f} {m['v2_brier']:>10.4f} "
            f"{d:>+10.4f} {flag:>10}"
        )

    print("\n🚦 Cutover gates:")
    g = summary["gates"]
    print(f"   {'✅' if g['brier_v2_better_than_mono'] else '❌'} v2 Brier < mono Brier")
    print(f"   {'✅' if g['n_events_ge_50'] else '❌'} n_events ≥ 50 (have {summary['n_events']})")
    print(f"   {'✅' if g['no_category_regression'] else '❌'} No category regression > 10%")

    print("\n🎯 Recommendation:")
    if g["all_passed"]:
        print("   ✅ ALL GATES PASSED — ready for HUMAN cutover decision")
        print("   To switch: edit .env on prod, set PIPELINE_VERSION=v2_two_stage, restart container")
    elif not g["n_events_ge_50"]:
        print(f"   ⏳ WAIT — need more data ({50 - summary['n_events']} more events)")
    else:
        print("   ❌ BLOCKED — fix issues above before cutover")


if __name__ == "__main__":
    asyncio.run(run())
