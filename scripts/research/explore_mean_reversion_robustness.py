#!/usr/bin/env python3
"""Robustness-чек для mean-reversion 20d/5d — ищем ловушки.

ROI_net +13% при WR 48.4% (<50%) — прибыль на асимметрии payoff. Проверяем,
не артефакт ли это:

  CHECK 1 — Resolution survivorship: какая доля сделок выходит по РЕЗОЛВУ
    (а не по 5d-close), и какая доля P&L оттуда. Если P&L держится на
    резолв-выходах → это НЕ 5d-mean-reversion, а «купил дёшево, дождался YES».

  CHECK 2 — Концентрация P&L: дают ли top-1%/top-5% сделок весь профит.
    Если да — стратегия неторгуема (нельзя предсказать «ту самую» сделку).

  CHECK 3 — Entry-price стратификация: где живёт edge — в глубоком overshoot
    (entry<0.10, fat-tail, fee-heavy) или в средней зоне. Quantpedia red-flag:
    fat-tail рынки под costs дают Sharpe<0.5.

  CHECK 4 — Exit БЕЗ резолв-survivorship: пересчёт ROI, выбросив все сделки,
    что дожили до резолва (только чистый 5d-close exit). Это «честное» ядро.

Запуск (на проде):
    cd ~/oracle && /root/oracle/.venv-research/bin/python \
        scripts/research/explore_mean_reversion_robustness.py
"""

from __future__ import annotations

import duckdb

CANONICAL = "/mnt/data/canonical"

_FEE_SCHEDULE = {
    "crypto": (0.072, 0.20), "sports": (0.030, 0.25), "finance": (0.040, 0.25),
    "politics": (0.040, 0.25), "economics": (0.050, 0.25), "culture": (0.050, 0.25),
    "weather": (0.050, 0.25), "tech": (0.040, 0.25), "mentions": (0.040, 0.25),
    "other": (0.050, 0.25), "geopolitics": (0.0, 0.25),
}
_DEFAULT_FEE = (0.050, 0.25)
LOOKBACK_DAYS, HOLD_DAYS = 20, 5
MIN_HORIZON_DAYS, MIN_VOLUME, STAKE = 90, 5000.0, 100.0


def fee(amount, price, category, is_maker):
    rate, rebate = _FEE_SCHEDULE.get((category or "").lower(), _DEFAULT_FEE)
    p = min(max(price, 1e-6), 1 - 1e-6)
    taker = amount * rate * (p * (1 - p))
    return taker * (1 - rebate) if is_maker else taker


# Тот же entries/exits SQL, но возвращаем ещё entry_px-зоны и флаг резолв-выхода.
SQL = f"""
WITH span AS (
    SELECT condition_id,
           (max(epoch(hour_bucket)) - min(epoch(hour_bucket)))/86400.0 AS days_span
    FROM read_parquet('{CANONICAL}/trade_buckets.parquet') GROUP BY condition_id
),
candidates AS (
    SELECT m.condition_id, lower(m.category) AS category, m.yes_won
    FROM read_parquet('{CANONICAL}/markets_clean.parquet') m
    JOIN span s ON s.condition_id = m.condition_id
    WHERE m.horizon_days >= {MIN_HORIZON_DAYS} AND m.yes_won IS NOT NULL
      AND m.lifetime_volume_usd >= {MIN_VOLUME}
      AND s.days_span >= {LOOKBACK_DAYS + HOLD_DAYS}
),
daily AS (
    SELECT t.condition_id,
           CAST(epoch(t.hour_bucket)/86400 AS BIGINT) AS day_idx,
           arg_max(t.vwap_yes, epoch(t.hour_bucket)) AS close_px
    FROM read_parquet('{CANONICAL}/trade_buckets.parquet') t
    JOIN candidates c ON c.condition_id = t.condition_id
    WHERE t.vwap_yes > 0.01 AND t.vwap_yes < 0.99
    GROUP BY t.condition_id, day_idx
),
rolling AS (
    SELECT d.*,
           row_number() OVER (PARTITION BY condition_id ORDER BY day_idx) AS rn,
           min(close_px) OVER (PARTITION BY condition_id ORDER BY day_idx
               ROWS BETWEEN {LOOKBACK_DAYS} PRECEDING AND 1 PRECEDING) AS roll_min
    FROM daily d
),
entries AS (
    SELECT r.condition_id, r.day_idx AS entry_day, r.close_px AS entry_px,
           c.category, c.yes_won
    FROM rolling r JOIN candidates c ON c.condition_id = r.condition_id
    WHERE r.rn > {LOOKBACK_DAYS} AND r.roll_min IS NOT NULL
      AND r.close_px <= r.roll_min AND r.close_px > 0.02
),
entries_cd AS (
    SELECT *, lag(entry_day) OVER (PARTITION BY condition_id ORDER BY entry_day) AS prev_entry
    FROM entries
),
entries_final AS (
    SELECT * FROM entries_cd WHERE prev_entry IS NULL OR entry_day - prev_entry >= {HOLD_DAYS}
),
exits AS (
    SELECT e.*,
           (SELECT min(d2.day_idx) FROM daily d2
            WHERE d2.condition_id = e.condition_id AND d2.day_idx >= e.entry_day + {HOLD_DAYS}) AS exit_day
    FROM entries_final e
)
SELECT x.category, x.entry_px, x.yes_won,
       (SELECT d3.close_px FROM daily d3
        WHERE d3.condition_id = x.condition_id AND d3.day_idx = x.exit_day) AS exit_px
FROM exits x
"""


def main():
    con = duckdb.connect()
    con.execute("PRAGMA threads=4")
    rows = con.execute(SQL).fetchall()
    print(f"Всего сделок: {len(rows):,}\n")

    trades = []
    for category, entry_px, yes_won, exit_px in rows:
        resolved_exit = exit_px is None
        exit_val = (1.0 if yes_won == 1 else 0.0) if resolved_exit else float(exit_px)
        entry = float(entry_px)
        shares = STAKE / entry
        gross = shares * (exit_val - entry)
        fee_in = fee(STAKE, entry, category, True)
        fee_out = 0.0 if resolved_exit else fee(shares * exit_val, exit_val, category, True)
        trades.append({
            "category": category, "entry": entry, "exit": exit_val,
            "resolved_exit": resolved_exit, "net": gross - fee_in - fee_out,
        })

    n = len(trades)
    total_net = sum(t["net"] for t in trades)

    # CHECK 1 — resolution survivorship
    print("=" * 64)
    print("CHECK 1 — Resolution survivorship")
    print("=" * 64)
    res = [t for t in trades if t["resolved_exit"]]
    clean = [t for t in trades if not t["resolved_exit"]]
    res_net = sum(t["net"] for t in res)
    clean_net = sum(t["net"] for t in clean)
    print(f"Сделок с выходом по РЕЗОЛВУ: {len(res):,} ({100*len(res)/n:.1f}%)")
    print(f"Сделок с чистым 5d-close exit: {len(clean):,} ({100*len(clean)/n:.1f}%)")
    print(f"P&L от резолв-выходов:  ${res_net:+,.0f} ({100*res_net/total_net:.1f}% всего P&L)")
    print(f"P&L от 5d-close выходов: ${clean_net:+,.0f} ({100*clean_net/total_net:.1f}% всего P&L)")
    if total_net > 0 and res_net / total_net > 0.5:
        print("⚠️  БОЛЬШЕ ПОЛОВИНЫ P&L от резолв-выходов → это НЕ 5d-mean-reversion,")
        print("    а 'купил дёшево, дождался YES'. Другой риск-профиль, look-ahead-подобно.")

    # CHECK 2 — концентрация P&L
    print("\n" + "=" * 64)
    print("CHECK 2 — Концентрация P&L (по net на сделку)")
    print("=" * 64)
    nets = sorted((t["net"] for t in trades), reverse=True)
    for pct in (0.01, 0.05, 0.10):
        k = max(1, int(n * pct))
        top_sum = sum(nets[:k])
        print(f"  top-{pct:.0%} сделок ({k}): ${top_sum:+,.0f} = {100*top_sum/total_net:.1f}% всего P&L")
    wins_sum = sum(x for x in nets if x > 0)
    loss_sum = sum(x for x in nets if x < 0)
    print(f"  Сумма всех ВЫИГРЫШЕЙ: ${wins_sum:+,.0f}")
    print(f"  Сумма всех ПРОИГРЫШЕЙ: ${loss_sum:+,.0f}")
    print(f"  Profit factor (wins/|loss|): {wins_sum/abs(loss_sum):.2f}")

    # CHECK 3 — entry-price стратификация
    print("\n" + "=" * 64)
    print("CHECK 3 — Где живёт edge (по entry-цене)")
    print("=" * 64)
    bands = [(0.02, 0.10, "deep <0.10"), (0.10, 0.25, "low 0.10-0.25"),
             (0.25, 0.50, "mid 0.25-0.50"), (0.50, 0.75, "high 0.50-0.75"),
             (0.75, 0.98, "top 0.75-0.98")]
    for lo, hi, label in bands:
        band = [t for t in trades if lo <= t["entry"] < hi]
        if not band:
            continue
        bn = len(band)
        bnet = sum(t["net"] for t in band)
        bwins = sum(1 for t in band if t["net"] > 0)
        roi = 100 * bnet / (bn * STAKE)
        print(f"  {label:16s} N={bn:5d}  WR={bwins/bn:.0%}  ROI_net={roi:+6.1f}%  "
              f"P&L=${bnet:+,.0f}")

    # CHECK 4 — честное ядро (без резолв-survivorship)
    print("\n" + "=" * 64)
    print("CHECK 4 — Честное ядро: ТОЛЬКО 5d-close exit (без резолв-survivorship)")
    print("=" * 64)
    cn = len(clean)
    if cn:
        cwins = sum(1 for t in clean if t["net"] > 0)
        croi = 100 * clean_net / (cn * STAKE)
        print(f"  N={cn:,}  WR(net)={cwins/cn:.1%}  ROI_net={croi:+.2f}%  "
              f"honest(×0.5)={croi*0.5:+.2f}%")
        print(f"  → ЭТО реальный 5-day mean-reversion edge (без 'дождались резолва').")
    else:
        print("  Нет чистых 5d-close сделок?!")


if __name__ == "__main__":
    main()
