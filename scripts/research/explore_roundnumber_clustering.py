#!/usr/bin/env python3
"""Round-number clustering exploration (research idea G2 / H2).

Гипотеза: трейды на Polymarket кластеризуются на round-number ценах
(0.10/0.50/0.90) сильнее случайного → momentum/reversion после пробоя.

КРИТИЧЕСКИЙ kill-критерий (tick-артефакт):
    Polymarket tick = 1c. Кластеризация на 0.50 может быть просто следствием
    дискретной сетки цен, а не поведением. Контр-тест:
      1. Посчитать долю трейд-объёма на ТОЧНЫХ round-уровнях {0.10,0.50,0.90}
         vs на соседних не-round центах ({0.49,0.51} vs 0.50 и т.д.).
      2. Если round-уровни НЕ выделяются над соседними центами → артефакт сетки,
         идея МЁРТВАЯ.

Источник цен: trade_buckets.parquet (hourly, vwap_yes + min/max + volume).
Это НЕ tick-level (нет отдельных трейдов), поэтому используем vwap_yes как
прокси модальной цены бакета, взвешенный по total_volume_usd. Это приближение —
честно отмечаем в выводе.

Запуск (на проде, где лежит canonical):
    cd ~/oracle && /root/oracle/.venv-research/bin/python \
        scripts/research/explore_roundnumber_clustering.py
"""

from __future__ import annotations

import duckdb

CANONICAL = "/mnt/data/canonical"


def main() -> None:
    con = duckdb.connect()
    tb = f"read_parquet('{CANONICAL}/trade_buckets.parquet')"

    print("=" * 70)
    print("ROUND-NUMBER CLUSTERING — tick-artefact контр-тест")
    print("=" * 70)

    # --- Шаг 1: распределение объёма по центовым уровням ---
    # vwap_yes округляем до цента, агрегируем total_volume_usd.
    # cent = round(vwap_yes * 100). Берём 1c..99c (исключаем 0 и 100 — резолв).
    rows = con.execute(
        f"""
        WITH cents AS (
            SELECT
                CAST(round(vwap_yes * 100) AS INTEGER) AS cent,
                total_volume_usd AS vol
            FROM {tb}
            WHERE vwap_yes > 0.005 AND vwap_yes < 0.995
              AND total_volume_usd > 0
        )
        SELECT cent, sum(vol) AS vol, count(*) AS n_buckets
        FROM cents
        GROUP BY cent
        ORDER BY cent
        """
    ).fetchall()

    by_cent = {c: (v, n) for c, v, n in rows}
    total_vol = sum(v for v, _ in by_cent.values())

    def vol_at(cent: int) -> float:
        return by_cent.get(cent, (0.0, 0))[0]

    def share(cent: int) -> float:
        return 100.0 * vol_at(cent) / total_vol if total_vol else 0.0

    # «Локальный baseline» — средний объём 2 соседей слева/справа (не считая сам).
    def neighbor_avg(cent: int, span: int = 3) -> float:
        neigh = [
            vol_at(c)
            for c in range(cent - span, cent + span + 1)
            if c != cent and 1 <= c <= 99
        ]
        return sum(neigh) / len(neigh) if neigh else 0.0

    print(f"\nВсего объёма (1c–99c): ${total_vol:,.0f}")
    print(f"Уникальных центовых уровней: {len(by_cent)}")
    print(f"\n{'level':>6} {'vol_share%':>11} {'vol_$':>16} "
          f"{'neighbor_avg_$':>16} {'ratio_vs_neigh':>15}")
    print("-" * 70)

    round_levels = [10, 50, 90]
    quarter_levels = [25, 75]
    decile_levels = [20, 30, 40, 60, 70, 80]

    def report(cent: int, tag: str) -> float:
        v = vol_at(cent)
        na = neighbor_avg(cent)
        ratio = v / na if na else float("inf")
        print(f"{cent/100:>6.2f} {share(cent):>11.3f} {v:>16,.0f} "
              f"{na:>16,.0f} {ratio:>14.2f}x  {tag}")
        return ratio

    print("# ROUND (главные round-числа):")
    round_ratios = [report(c, "ROUND") for c in round_levels]
    print("# QUARTER:")
    for c in quarter_levels:
        report(c, "quarter")
    print("# DECILE:")
    for c in decile_levels:
        report(c, "decile")

    # Контрольные не-round центы (должны быть ~1.0x к соседям).
    print("# CONTROL (произвольные не-round центы — ожидаем ~1.0x):")
    control_ratios = [report(c, "control") for c in (17, 33, 43, 57, 63, 83)]

    # --- Шаг 2: вердикт по tick-артефакту ---
    print("\n" + "=" * 70)
    print("ВЕРДИКТ — tick-артефакт?")
    print("=" * 70)
    import statistics

    avg_round = statistics.mean(round_ratios)
    avg_control = statistics.mean(control_ratios)
    print(f"Средний ratio_vs_neighbor для ROUND {{0.10,0.50,0.90}}: {avg_round:.2f}x")
    print(f"Средний ratio_vs_neighbor для CONTROL (не-round):       {avg_control:.2f}x")

    if avg_round < 1.30:
        print("\n❌ KILL: round-уровни НЕ выделяются над соседними центами "
              "(<1.3x). Кластеризация = артефакт сетки/шум. Идея мёртвая.")
    elif avg_round < avg_control * 1.5:
        print("\n⚠️  WEAK: round чуть выше, но не радикально над control. "
              "Маргинально, fee-sensitive — низкий приоритет.")
    else:
        print("\n✅ SIGNAL: round-уровни существенно выделяются над "
              "соседними центами И над control. Есть behavioral clustering — "
              "стоит идти на шаг 2 (momentum/reversion после пробоя).")

    # --- Шаг 3: грубая оценка масштаба (сколько рынков реально round-heavy) ---
    print("\n" + "=" * 70)
    print("МАСШТАБ — на скольких рынках 0.50 заметно доминирует")
    print("=" * 70)
    scale = con.execute(
        f"""
        WITH per_market AS (
            SELECT
                condition_id,
                sum(total_volume_usd) AS tot,
                sum(CASE WHEN CAST(round(vwap_yes*100) AS INT)=50
                         THEN total_volume_usd ELSE 0 END) AS at_50
            FROM {tb}
            WHERE vwap_yes > 0.005 AND vwap_yes < 0.995
            GROUP BY condition_id
            HAVING sum(total_volume_usd) > 1000
        )
        SELECT
            count(*) AS n_markets,
            count(*) FILTER (WHERE at_50/tot > 0.10) AS n_50_heavy,
            avg(at_50/tot) AS avg_50_share
        FROM per_market
        """
    ).fetchone()
    print(f"Рынков с vol>$1K: {scale[0]:,}")
    print(f"Из них с >10% объёма на 0.50: {scale[1]:,} "
          f"({100*scale[1]/scale[0]:.1f}%)")
    print(f"Средняя доля объёма на 0.50: {100*scale[2]:.2f}%")


if __name__ == "__main__":
    main()
