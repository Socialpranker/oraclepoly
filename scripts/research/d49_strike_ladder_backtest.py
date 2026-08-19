"""D49 strike-ladder monotonicity arb — canonical backtest (W2.1 роадмапа, 2026-07-09).

Гипотеза: для крипто-лесенки "BTC above $K by <same date>" обязано
P(>K1) >= P(>K2) при K1 < K2 (более высокий страйк не может быть вероятнее
более низкого при прочих равных). Нарушение = model-free арбитраж:
BUY YES(>K1) + BUY NO(>K2), суммарная стоимость p1 + (1 - p2) < 1 при p2 > p1,
payoff >= $1 при ЛЮБОМ исходе (в отличие от D44/D42 это НЕ fair-value ставка,
а внутренняя консистентность рынка — сиблинг D3, не заблокирован Kronos NO-GO).

Шаг 0 (сделан отдельно, prod SQL): все крипто strike-рынки в БД имеют
neg_risk=false — нулевое пересечение с D3 (который сканирует
neg_risk=true AND neg_risk_other=false, см. app/jobs/d3_scan.py:41-42).
D49 работает в непересекающемся скоупе.

Матчер вопросов переиспользует прецедент scripts/research/calendar_spread_matcher.py
(regex on question/slug), но не сам matcher (тот матчит РАЗНЫЕ даты одного strike;
D49 матчит РАЗНЫЕ strikes одной даты) — здесь отдельный `_STRIKE_RE`/`_DATE_RE`
аналогичного духа: строгий regex, best-effort=None лучше, чем неверная пара.

Запуск (на VPS, research-venv с duckdb, ИМЕЕТ доступ к app.* из /root/oracle):
    /root/oracle/.venv-research/bin/python -m scripts.research.d49_strike_ladder_backtest \
        --canonical /mnt/data/canonical --out research_out/d49_strike_ladder.md

Anchors в price_snapshots.parquet: hours_to_resolve in {4,8,12,24,48,72,168,720}.
"""

from __future__ import annotations

import argparse
import re
import sys
from collections import defaultdict
from dataclasses import dataclass
from datetime import date, datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))

import duckdb  # noqa: E402

from app.backtest.metrics import wilson_score_interval  # noqa: E402
from app.services.fee_calculator import estimate_trading_fee  # noqa: E402

DEFAULT_CANONICAL = Path("/mnt/data/canonical")
ANCHORS = [4, 8, 12, 24, 48, 72, 168, 720]

# Conservative slippage haircut (roadmap: "2.5% fee + 1-2pp slippage" если
# нет per-category константы под рукой) — по одной ноге пары, суммарно 2 ноги.
SLIPPAGE_PP_PER_LEG = 1.5

_ASSET_ALIASES: dict[str, str] = {
    "bitcoin": "BTC",
    "btc": "BTC",
    "ethereum": "ETH",
    "eth": "ETH",
    "solana": "SOL",
    "sol": "SOL",
    "xrp": "XRP",
    "ripple": "XRP",
    "dogecoin": "DOGE",
    "doge": "DOGE",
}
_ASSET_RE = re.compile(
    r"\b(" + "|".join(sorted(_ASSET_ALIASES, key=len, reverse=True)) + r")\b",
    re.IGNORECASE,
)

# СТРОГО однонаправленные "above/greater than/over/exceed/surpass $X" — валидная
# лесенка P(>K), монотонно убывающая по K.
#
# "reach"/"hit" ИСКЛЮЧЕНЫ намеренно (verified 2026-07-09 на SOL "reach $130/$250
# in November 2024": обе резолвились по touch-anytime семантике — $130 ниже
# спота на момент создания рынка = downside-touch вопрос ("упадёт ли до $130"),
# $250 выше спота = upside-touch ("вырастет ли до $250"). Это НЕ P(>K) лесенка —
# у touch-anytime барьеров по разные стороны от спота нет обязательной
# монотонности, гипотеза D49 (P(>K1)>=P(>K2)) на них не применима. "above"/
# "greater than" по конструкции всегда P(текущая_цена_на_резолве > K) —
# однозначно однонаправленные, безопасны для гипотезы.
_STRIKE_RE = re.compile(
    r"\b(?:above|over|greater than|exceed|surpass)\s*\$\s*"
    r"(?P<amount>[\d,]+(?:\.\d+)?)\s*(?P<suffix>k|m|b|billion|trillion)?\b",
    re.IGNORECASE,
)

_EXCLUDE_RE = re.compile(
    r"\bdip to\b|\bless than\b|\bbetween\b|\bbelow\b|\bor\s+\$|\breach\b|\bhit\b",
    re.IGNORECASE,
)

# Нормализация "хвоста" вопроса (всё после $strike) в ключ окна резолва.
# КРИТИЧНО (verified 2026-07-09 на BTC 2025-09-01: "reach $116K August 25-31"
# won=0 vs "reach $120K in August" won=1 — при одинаковом canonical end_date):
# один и тот же end_date может покрывать РАЗНЫЕ окна наблюдения ("in August" —
# touch anytime за месяц, "August 25-31" — touch anytime за неделю, "on October 3"
# — snapshot в конкретный момент). Смешивание окон даёт non-monotonic пары,
# которые на самом деле не арбитраж, а разные ставки. Числа в хвосте (страйк
# уже вырезан) заменяем плейсхолдером, чтобы "on May 30 at 5 PM ET" не различался
# по нерелевантным цифрам, но при этом реальные разные даты/окна ("in August" vs
# "August 25-31" vs "on October 3") остаются РАЗНЫМИ ключами.
_STRIP_STRIKE_RE = re.compile(
    r"\b(?:above|over|greater than|exceed|surpass)\s*\$\s*[\d,]+(?:\.\d+)?\s*(?:k|m|b|billion|trillion)?\b",
    re.IGNORECASE,
)


def _resolution_window_key(question: str) -> str:
    """Хвост вопроса после вырезания strike-фразы, lowercased+trimmed — прокси
    окна резолва. Пары матчатся только при одинаковом ключе (см. docstring выше)."""
    tail = _STRIP_STRIKE_RE.sub("", question)
    tail = tail.strip().rstrip("?").strip().lower()
    return tail


@dataclass(frozen=True)
class ParsedStrike:
    entity: str
    strike: float
    end_date: date
    condition_id: str
    question: str
    window_key: str


def _parse_strike_amount(m: re.Match) -> float:
    amount = float(m.group("amount").replace(",", ""))
    suffix = (m.group("suffix") or "").lower()
    if suffix == "k":
        amount *= 1_000
    elif suffix == "m":
        amount *= 1_000_000
    elif suffix in ("b", "billion"):
        amount *= 1_000_000_000
    elif suffix == "trillion":
        amount *= 1_000_000_000_000
    return amount


def match_strike_ladder_market(*, question: str, end_date: date, condition_id: str) -> ParsedStrike | None:
    """Парсит "Will BTC be above $100,000 on October 3?" -> ParsedStrike.

    Дата резолва берётся из canonical markets_clean.end_date (точный источник,
    не текстовый парсинг) — вопросы используют разнородные текстовые форматы дат
    ("on <Date>", "on <Date> at <Time> ET", "in <Month>", "by <Date>", "<range>-<range>",
    "today"), большинство без явного года; парсить их regex'ом ненадёжно и не нужно,
    end_date уже даёт точный canonical resolution timestamp для группировки лесенки.

    None (не best-effort) для range-вопросов ("between $X and $Y"), обратного
    направления ("below"/"less than"/"dip to"), гонок "X or $Y", отсутствия
    распознанного актива/strike — двусмысленность здесь стоит реальных денег
    (ложная лесенка = legging risk на арбитраже).
    """
    if _EXCLUDE_RE.search(question):
        return None

    asset_match = _ASSET_RE.search(question)
    if asset_match is None:
        return None
    entity = _ASSET_ALIASES[asset_match.group(1).lower()]

    strike_match = _STRIKE_RE.search(question)
    if strike_match is None:
        return None
    strike = _parse_strike_amount(strike_match)
    window_key = _resolution_window_key(question)

    return ParsedStrike(
        entity=entity, strike=strike, end_date=end_date, condition_id=condition_id,
        question=question, window_key=window_key,
    )


def load_markets(con: duckdb.DuckDBPyConnection, canonical: Path) -> list[dict]:
    path = str(canonical / "markets_clean.parquet")
    rows = con.execute(
        f"""
        SELECT condition_id, question, event_id, category, neg_risk, end_date,
               lifetime_volume_usd, horizon_days, yes_won, n_trades_total
        FROM read_parquet('{path}')
        WHERE regexp_matches(question, '(?i)\\$\\s*[0-9]')
          AND regexp_matches(question, '(?i)\\b(bitcoin|btc|ethereum|eth|solana|sol|xrp|ripple|dogecoin|doge)\\b')
        """
    ).fetchall()
    cols = [d[0] for d in con.description]
    return [dict(zip(cols, r, strict=True)) for r in rows]


def build_ladders(markets: list[dict]) -> dict[tuple[str, date, str], list[ParsedStrike]]:
    """Группирует по (entity, end_date, window_key), лесенка = >=2 разных страйков.

    window_key (см. _resolution_window_key) обязателен в ключе группировки —
    без него лесенка склеивает рынки с разным окном наблюдения ("in August" vs
    "August 25-31" при одинаковом canonical end_date), что даёт ложные
    non-monotonic пары (verified 2026-07-09, см. docstring _resolution_window_key).
    """
    groups: dict[tuple[str, date, str], list[ParsedStrike]] = defaultdict(list)
    for m in markets:
        end_date_raw = m["end_date"]
        if end_date_raw is None:
            continue
        end_date = end_date_raw.date() if isinstance(end_date_raw, datetime) else end_date_raw
        parsed = match_strike_ladder_market(
            question=m["question"] or "", end_date=end_date, condition_id=m["condition_id"]
        )
        if parsed is None:
            continue
        groups[(parsed.entity, parsed.end_date, parsed.window_key)].append(parsed)

    ladders: dict[tuple[str, date, str], list[ParsedStrike]] = {}
    for key, items in groups.items():
        # dedupe по strike: если на тот же (entity, end_date, window_key, strike)
        # несколько condition_id с РАЗНЫМ вопросом — это разные рынки, случайно
        # схлопнувшиеся в один strike-слот текстовым парсингом (не должно
        # происходить при точном end_date, но подстрахуемся) — пропускаем
        # весь strike, не гадаем какой condition_id "правильный".
        by_strike: dict[float, list[ParsedStrike]] = defaultdict(list)
        for it in items:
            by_strike[it.strike].append(it)
        distinct: list[ParsedStrike] = []
        for strike, candidates in by_strike.items():
            if len({c.condition_id for c in candidates}) > 1:
                continue
            distinct.append(candidates[0])
        distinct.sort(key=lambda p: p.strike)
        if len(distinct) >= 2:
            ladders[key] = distinct
    return ladders


def load_price_snapshots(con: duckdb.DuckDBPyConnection, canonical: Path, condition_ids: set[str]) -> dict[tuple[str, int], float]:
    """condition_id -> {hours_to_resolve -> yes_price}. Один снапшот на (id, anchor):
    берём последний по trade_ts внутри бакета (canonical уже агрегирует по анкорам,
    но подстрахуемся GROUP BY на случай дублей)."""
    path = str(canonical / "price_snapshots.parquet")
    ids_list = ",".join(f"'{cid}'" for cid in condition_ids)
    if not ids_list:
        return {}
    rows = con.execute(
        f"""
        SELECT condition_id, hours_to_resolve, avg(yes_price) AS yes_price
        FROM read_parquet('{path}')
        WHERE condition_id IN ({ids_list})
        GROUP BY 1, 2
        """
    ).fetchall()
    out: dict[tuple[str, int], float] = {}
    for cid, hrs, price in rows:
        out[(cid, hrs)] = float(price)
    return out


@dataclass
class ViolationRow:
    entity: str
    end_date: date
    anchor_h: int
    k1: float
    k2: float
    p1: float  # P(YES > K1)
    p2: float  # P(YES > K2), должно быть <= p1
    net_spread: float  # p1 - p2, положительное = валидное, отрицательное = нарушение направления, здесь считаем raw p1-p2 до fee
    net_edge_after_fees: float  # (p1 - p2) - fees_both_legs - slippage, >0 = profitable arb
    fee_leg1: float
    fee_leg2: float
    yes_won_k1: int | None
    yes_won_k2: int | None
    volume_k1: float | None
    volume_k2: float | None
    category: str | None


def evaluate_ladders(
    ladders: dict[tuple[str, date, str], list[ParsedStrike]],
    markets_by_cid: dict[str, dict],
    snapshots: dict[tuple[str, int], float],
) -> list[ViolationRow]:
    rows: list[ViolationRow] = []
    for (entity, end_date, _window_key), rungs in ladders.items():
        # Все пары K1 < K2 в лесенке (не только соседние — вся лестница монотонна).
        for i in range(len(rungs)):
            for j in range(i + 1, len(rungs)):
                r1, r2 = rungs[i], rungs[j]  # r1.strike < r2.strike
                m1 = markets_by_cid.get(r1.condition_id, {})
                m2 = markets_by_cid.get(r2.condition_id, {})
                category = m1.get("category") or m2.get("category")
                for anchor_h in ANCHORS:
                    p1 = snapshots.get((r1.condition_id, anchor_h))
                    p2 = snapshots.get((r2.condition_id, anchor_h))
                    if p1 is None or p2 is None:
                        continue
                    if not (0.0 < p1 < 1.0 and 0.0 < p2 < 1.0):
                        continue
                    # Стратегия: BUY YES(K1) @ p1 + BUY NO(K2) @ (1-p2).
                    # Cost = p1 + (1-p2). Profitable model-free arb требует p2 > p1
                    # (т.е. cost < 1 гарантированно). Пишем raw net_spread = p1-p2
                    # (положительное = market валиден и монотонен; отрицательное =
                    # нарушение, p2>p1, это и есть возможность).
                    net_spread = p1 - p2
                    fee_leg1 = estimate_trading_fee(1.0, p1, category or "other")  # YES(K1) buy
                    fee_leg2 = estimate_trading_fee(1.0, 1.0 - p2, category or "other")  # NO(K2) buy priced at 1-p2
                    fees_total_pct = (fee_leg1 + fee_leg2) * 100.0  # already per $1 notional -> pct points
                    slippage_pct = SLIPPAGE_PP_PER_LEG * 2  # оба leg'а
                    # net_edge = (p2 - p1) - fees - slippage, положительное = arb net-profitable
                    net_edge_after_fees = (p2 - p1) * 100.0 - fees_total_pct - slippage_pct
                    rows.append(
                        ViolationRow(
                            entity=entity,
                            end_date=end_date,
                            anchor_h=anchor_h,
                            k1=r1.strike,
                            k2=r2.strike,
                            p1=p1,
                            p2=p2,
                            net_spread=net_spread,
                            net_edge_after_fees=net_edge_after_fees,
                            fee_leg1=fee_leg1,
                            fee_leg2=fee_leg2,
                            yes_won_k1=m1.get("yes_won"),
                            yes_won_k2=m2.get("yes_won"),
                            volume_k1=m1.get("lifetime_volume_usd"),
                            volume_k2=m2.get("lifetime_volume_usd"),
                            category=category,
                        )
                    )
    return rows


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--canonical", default=str(DEFAULT_CANONICAL))
    parser.add_argument("--out", default=None, help="optional path to dump raw CSV of violation rows")
    parser.add_argument("--live-start", default="2025-01-01", help="cutoff for 'живой период' срез в вердикте")
    args = parser.parse_args()

    canonical = Path(args.canonical)
    con = duckdb.connect()

    markets = load_markets(con, canonical)
    print(f"[d49] crypto strike-question candidate rows: {len(markets)}", file=sys.stderr)

    ladders = build_ladders(markets)
    n_pairs = sum(len(v) * (len(v) - 1) // 2 for v in ladders.values())
    print(f"[d49] ladders (entity,end_date) with >=2 strikes: {len(ladders)}, total strike-pairs: {n_pairs}", file=sys.stderr)

    all_cids: set[str] = set()
    for rungs in ladders.values():
        for r in rungs:
            all_cids.add(r.condition_id)
    snapshots = load_price_snapshots(con, canonical, all_cids)
    print(f"[d49] snapshot rows loaded for {len(all_cids)} condition_ids: {len(snapshots)}", file=sys.stderr)

    markets_by_cid = {m["condition_id"]: m for m in markets}
    rows = evaluate_ladders(ladders, markets_by_cid, snapshots)
    print(f"[d49] evaluated (pair, anchor) observations: {len(rows)}", file=sys.stderr)

    if args.out:
        import csv

        with open(args.out, "w", newline="") as f:
            w = csv.writer(f)
            w.writerow(
                [
                    "entity", "end_date", "anchor_h", "k1", "k2", "p1", "p2",
                    "net_spread", "net_edge_after_fees_pct", "fee_leg1", "fee_leg2",
                    "yes_won_k1", "yes_won_k2", "volume_k1", "volume_k2", "category",
                ]
            )
            for r in rows:
                w.writerow(
                    [
                        r.entity, r.end_date, r.anchor_h, r.k1, r.k2, r.p1, r.p2,
                        r.net_spread, r.net_edge_after_fees, r.fee_leg1, r.fee_leg2,
                        r.yes_won_k1, r.yes_won_k2, r.volume_k1, r.volume_k2, r.category,
                    ]
                )
        print(f"[d49] wrote raw rows to {args.out}", file=sys.stderr)

    # Summary stats to stdout (parsed by caller / included manually in report).
    n_obs = len(rows)
    n_violations_raw = sum(1 for r in rows if r.net_spread < 0)
    n_net_profitable = sum(1 for r in rows if r.net_edge_after_fees > 0)
    print(f"n_observations={n_obs}")
    print(f"n_raw_monotonicity_violations={n_violations_raw} ({n_violations_raw / n_obs * 100:.2f}%)" if n_obs else "n_raw_monotonicity_violations=0")
    print(f"n_net_profitable_after_fees={n_net_profitable} ({n_net_profitable / n_obs * 100:.2f}%)" if n_obs else "n_net_profitable_after_fees=0")

    if n_obs:
        wins = n_net_profitable
        lower, upper = wilson_score_interval(wins, n_obs, 0.95)
        print(f"wilson_95_net_profitable_rate=({lower:.4f}, {upper:.4f})")

    # "живой период" срез — уникальные пары (entity,k1,k2,end_date), не (pair,anchor) obs
    live_start = datetime.strptime(args.live_start, "%Y-%m-%d").date()
    live_pairs = {
        (r.entity, r.k1, r.k2, r.end_date)
        for r in rows
        if r.end_date >= live_start and r.net_edge_after_fees > 0
    }
    all_end_dates = [r.end_date for r in rows if r.end_date >= live_start]
    if all_end_dates:
        span_days = (max(all_end_dates) - min(all_end_dates)).days or 1
        signals_per_day = len(live_pairs) / span_days
        print(f"live_period_start={live_start} span_days={span_days}")
        print(f"live_period_distinct_net_profitable_pairs={len(live_pairs)}")
        print(f"live_period_signals_per_day={signals_per_day:.4f}")
    else:
        print(f"live_period_start={live_start} — no observations in window")

    if n_obs:
        spreads = sorted(r.net_edge_after_fees for r in rows)
        median_spread = spreads[len(spreads) // 2]
        print(f"median_net_edge_after_fees_pct={median_spread:.4f}")

    return 0


if __name__ == "__main__":
    sys.exit(main())
