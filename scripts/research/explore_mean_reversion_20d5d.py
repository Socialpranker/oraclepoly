#!/usr/bin/env python3
"""Mean-reversion 20d/5d backtest (research idea G1 / H5).

Гипотеза (Quantpedia, Polymarket binary contracts):
  Долгогоризонтные рынки систематически overshoot вниз → возвращаются к fair.
  Entry: текущая daily-close цена <= rolling 20-day min (из ПРОШЛЫХ дней).
  Exit:  фиксированно через 5 дней (или резолв, если раньше).
  Сторона: BUY YES (ставим на возврат вверх).

ВАЖНО про данные:
  price_snapshots.parquet — anchor-based, НЕ daily. Daily-сетку строим из
  trade_buckets.parquet (hourly vwap_yes): daily_close = последний vwap дня (UTC).
  Вся стратегия векторизована в SQL (window-функции), без Python-цикла по рынкам.

Честность (память: canonical СИСТЕМАТИЧЕСКИ переоценивает vs прод):
  - net P&L реальной fee-моделью проекта (per-category, maker rebate).
  - выводим И raw, И honest-haircut (×0.5 на edge) — это SHADOW-кандидат, не live.
  - mean-reversion = MAKER (limit на возврате) → is_maker=True.
  - no look-ahead: rolling-min строго из 20 ПРЕДЫДУЩИХ дней (не включая текущий).
  - 1 позиция за раз на рынок (не входим, пока не вышли) — через cooldown-фильтр.

Запуск (на проде):
    cd ~/oracle && /root/oracle/.venv-research/bin/python \
        scripts/research/explore_mean_reversion_20d5d.py
"""

from __future__ import annotations

import math

import duckdb

CANONICAL = "/mnt/data/canonical"

_FEE_SCHEDULE = {
    "crypto": (0.072, 0.20), "sports": (0.030, 0.25), "finance": (0.040, 0.25),
    "politics": (0.040, 0.25), "economics": (0.050, 0.25), "culture": (0.050, 0.25),
    "weather": (0.050, 0.25), "tech": (0.040, 0.25), "mentions": (0.040, 0.25),
    "other": (0.050, 0.25), "geopolitics": (0.0, 0.25),
}
_DEFAULT_FEE = (0.050, 0.25)

LOOKBACK_DAYS = 20
HOLD_DAYS = 5
MIN_HORIZON_DAYS = 90
MIN_VOLUME = 5000.0
STAKE = 100.0
HONEST_HAIRCUT = 0.5


def fee(amount: float, price: float, category: str, is_maker: bool) -> float:
    rate, rebate = _FEE_SCHEDULE.get((category or "").lower(), _DEFAULT_FEE)
    p = min(max(price, 1e-6), 1 - 1e-6)
    taker = amount * rate * (p * (1 - p))
    return taker * (1 - rebate) if is_maker else taker


def wilson_lo(wins: int, n: int, z: float = 1.96) -> float:
    if n == 0:
        return 0.0
    phat = wins / n
    denom = 1 + z * z / n
    centre = phat + z * z / (2 * n)
    margin = z * math.sqrt((phat * (1 - phat) + z * z / (4 * n)) / n)
    return (centre - margin) / denom


def main() -> None:
    con = duckdb.connect()
    con.execute("PRAGMA threads=4")
    mk = f"read_parquet('{CANONICAL}/markets_clean.parquet')"
    tb = f"read_parquet('{CANONICAL}/trade_buckets.parquet')"

    print("=" * 72)
    print(f"MEAN-REVERSION {LOOKBACK_DAYS}d-min / {HOLD_DAYS}d-hold — long-horizon")
    print("=" * 72)

    # Один большой SQL:
    #  1) candidates — long-horizon resolved liquid markets.
    #  2) daily — daily-close (последний vwap дня) только по кандидатам.
    #  3) rolling — rolling-20d-min из ПРОШЛЫХ дней (ROWS 20 PRECEDING ... 1 PRECEDING).
    #  4) entries — дни где close <= roll_min (overshoot вниз).
    #  5) exit — daily-close через >=HOLD_DAYS, либо резолв (yes_won) если выходим за конец.
    # Cooldown (1 позиция за раз) приблизим: между входами одного рынка >= HOLD_DAYS дней
    # (LAG-фильтр) — консервативно убирает overlapping re-entry.
    sql = f"""
    WITH span AS (
        SELECT condition_id,
               (max(epoch(hour_bucket)) - min(epoch(hour_bucket)))/86400.0 AS days_span
        FROM {tb} GROUP BY condition_id
    ),
    candidates AS (
        SELECT m.condition_id, lower(m.category) AS category, m.yes_won,
               m.horizon_days, m.lifetime_volume_usd
        FROM {mk} m JOIN span s ON s.condition_id = m.condition_id
        WHERE m.horizon_days >= {MIN_HORIZON_DAYS}
          AND m.yes_won IS NOT NULL
          AND m.lifetime_volume_usd >= {MIN_VOLUME}
          AND s.days_span >= {LOOKBACK_DAYS + HOLD_DAYS}
    ),
    daily AS (
        SELECT t.condition_id,
               CAST(epoch(t.hour_bucket) / 86400 AS BIGINT) AS day_idx,
               arg_max(t.vwap_yes, epoch(t.hour_bucket)) AS close_px
        FROM {tb} t
        JOIN candidates c ON c.condition_id = t.condition_id
        WHERE t.vwap_yes > 0.01 AND t.vwap_yes < 0.99
        GROUP BY t.condition_id, day_idx
    ),
    rolling AS (
        SELECT d.*,
               row_number() OVER (PARTITION BY condition_id ORDER BY day_idx) AS rn,
               min(close_px) OVER (
                   PARTITION BY condition_id ORDER BY day_idx
                   ROWS BETWEEN {LOOKBACK_DAYS} PRECEDING AND 1 PRECEDING
               ) AS roll_min
        FROM daily d
    ),
    entries AS (
        SELECT r.condition_id, r.day_idx AS entry_day, r.close_px AS entry_px,
               c.category, c.yes_won
        FROM rolling r
        JOIN candidates c ON c.condition_id = r.condition_id
        WHERE r.rn > {LOOKBACK_DAYS}                 -- достаточно истории
          AND r.roll_min IS NOT NULL
          AND r.close_px <= r.roll_min               -- overshoot вниз
          AND r.close_px > 0.02
    ),
    entries_cooldown AS (
        -- 1 позиция за раз: вход только если прошлый вход того же рынка >= HOLD_DAYS назад
        SELECT *,
               lag(entry_day) OVER (PARTITION BY condition_id ORDER BY entry_day) AS prev_entry
        FROM entries
    ),
    entries_final AS (
        SELECT * FROM entries_cooldown
        WHERE prev_entry IS NULL OR entry_day - prev_entry >= {HOLD_DAYS}
    ),
    exits AS (
        SELECT e.*,
               (SELECT min(d2.day_idx) FROM daily d2
                WHERE d2.condition_id = e.condition_id
                  AND d2.day_idx >= e.entry_day + {HOLD_DAYS}) AS exit_day
        FROM entries_final e
    )
    SELECT x.condition_id, x.category, x.entry_px, x.yes_won,
           x.exit_day,
           (SELECT d3.close_px FROM daily d3
            WHERE d3.condition_id = x.condition_id AND d3.day_idx = x.exit_day) AS exit_px
    FROM exits x
    """
    rows = con.execute(sql).fetchall()
    print(f"Сделок сгенерировано: {len(rows):,}")
    if not rows:
        print("Нет сделок — выход.")
        return

    trades: list[dict] = []
    for cond_id, category, entry_px, yes_won, exit_day, exit_px in rows:
        resolved_exit = exit_px is None
        exit_val = (1.0 if yes_won == 1 else 0.0) if resolved_exit else float(exit_px)
        trades.append({
            "category": category, "entry": float(entry_px),
            "exit": exit_val, "resolved_exit": resolved_exit,
        })

    def pnl_net(t: dict) -> float:
        shares = STAKE / t["entry"]
        gross = shares * (t["exit"] - t["entry"])
        fee_in = fee(STAKE, t["entry"], t["category"], True)
        fee_out = 0.0 if t["resolved_exit"] else fee(shares * t["exit"], t["exit"], t["category"], True)
        return gross - fee_in - fee_out

    def summarize(rows_: list[dict], label: str) -> None:
        n = len(rows_)
        if n == 0:
            return
        wins = sum(1 for t in rows_ if pnl_net(t) > 0)
        gross_sum = sum((STAKE / t["entry"]) * (t["exit"] - t["entry"]) for t in rows_)
        net_sum = sum(pnl_net(t) for t in rows_)
        roi_gross = 100.0 * gross_sum / (n * STAKE)
        roi_net = 100.0 * net_sum / (n * STAKE)
        print(f"\n[{label}] N={n}")
        print(f"  WR(net)={wins/n:.1%}  Wilson_lo(95%)={wilson_lo(wins, n):.1%}")
        print(f"  ROI gross={roi_gross:+.2f}%  net(maker)={roi_net:+.2f}%  "
              f"honest(×{HONEST_HAIRCUT})={roi_net*HONEST_HAIRCUT:+.2f}%")
        print(f"  Net P&L=${net_sum:+,.0f} (stake ${STAKE}/trade)")

    summarize(trades, "ALL")

    cats: dict[str, list[dict]] = {}
    for t in trades:
        cats.setdefault(t["category"], []).append(t)
    for cat in sorted(cats, key=lambda c: -len(cats[c])):
        if len(cats[cat]) >= 40:
            summarize(cats[cat], f"category={cat}")

    print("\n" + "=" * 72)
    print("ВЕРДИКТ")
    print("=" * 72)
    n = len(trades)
    wins = sum(1 for t in trades if pnl_net(t) > 0)
    net_total = sum(pnl_net(t) for t in trades)
    roi_net = 100.0 * net_total / (n * STAKE)
    print(f"Overall: N={n}, WR(net)={wins/n:.1%}, Wilson_lo={wilson_lo(wins, n):.1%}, "
          f"ROI_net={roi_net:+.2f}%, honest={roi_net*HONEST_HAIRCUT:+.2f}%")
    print("\nНапоминание: canonical-backtest СИСТЕМАТИЧЕСКИ переоценивает vs прод")
    print("(D44/D5/D31 были backtest-KEEPER, в проде убыточны). Любой keep =")
    print("кандидат в SHADOW, не live. honest = ×0.5 haircut на edge.")


if __name__ == "__main__":
    main()
