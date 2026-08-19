"""D49 StrikeLadder scan — каждые 5 минут, SHADOW ONLY (стадия по вердикту
бэктеста `research/d49-strike-ladder-2026-07-09/answer.md`).

Кандидаты: активные не-resolved крипто-рынки (или question похож на активы,
neg_risk=false — non-overlap с D3, см. docstring app/detectors/d49_strike_ladder.py)
с question ILIKE-префильтром на strike-фразы; точный парсинг — в Python
через strike_ladder_matcher (regex не должен жить в SQL).

Матчер группирует кандидатов в лесенки (entity, end_date, window_key), затем
для каждой пары рангов K1<K2 внутри лесенки build_arb() проверяет net-профитность
после fee+slippage. Нарушение → 2 сигнала (YES K1-нога, NO K2-нога) с общим
pair_key в evidence.

Идемпотентность: tx_hash = bucket_tx_hash(pair_key+leg, namespace="d49") —
один сигнал на пару-ногу за дедуп-окно (4ч, дефолт bucket_tx_hash). Enforced
на уровне БД partial UNIQUE-индексом uq_detector_signals_d49_tx
(migrations/20260710130000_d49_idempotency_index.sql) — без него ON CONFLICT
DO NOTHING в репозитории матчил бы только PK и дедуп не работал бы.

Книги для исполнимости (best bid/ask+size) кладутся в evidence best-effort —
на shadow-стадии execution_safety.evaluate их не читает, отсутствие книги
(None) не блокирует эмит, только снижает диагностическую ценность сигнала.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

import structlog

from app.detectors import d49_strike_ladder
from app.detectors.contract import Signal
from app.detectors.d49_strike_ladder import LadderLeg
from app.detectors.strike_ladder_matcher import ParsedStrike, build_ladders
from app.detectors.token_resolver import no_token_id, yes_token_id
from app.repositories.detector_signal_repository import get_detector_signal_repository
from app.services.database import fetch
from app.services.detector_helpers import bucket_tx_hash
from app.services.execution_safety import fetch_live_book
from app.services.live_prices import get_live_yes_price

logger = structlog.get_logger()


# Non-overlap с D3 (verified в бэктесте, шаг 0, prod SQL): все крипто strike-рынки
# в БД имеют neg_risk=false. D3 сканирует neg_risk=true AND neg_risk_other=false
# (app/jobs/d3_scan.py) — непересекающийся скоуп, e.neg_risk=false здесь — явный
# документирующий гейт, не защита от гонки с D3.
_CANDIDATE_MARKETS_SQL = """
SELECT m.polymarket_id AS market_id,
       m.condition_id,
       m.category,
       m.question,
       m.current_price,
       m.end_date,
       m.clob_token_ids,
       m.volume_24h
  FROM markets m
  LEFT JOIN events e ON e.id = m.event_id
 WHERE m.is_active = true
   AND m.resolved = false
   AND m.current_price IS NOT NULL
   AND m.end_date IS NOT NULL
   AND m.end_date > now()
   AND COALESCE(e.neg_risk, false) = false
   AND (m.category = 'Crypto'
        OR m.question ~* '\\b(bitcoin|btc|ethereum|eth|solana|sol|xrp|ripple|dogecoin|doge)\\b')
   AND (m.question ILIKE '%above%'
        OR m.question ILIKE '%exceed%'
        OR m.question ILIKE '%surpass%'
        OR m.question ILIKE '%over $%'
        OR m.question ILIKE '%greater than%')
 LIMIT 3000
"""


async def _resolve_live_price(row: dict[str, Any]) -> tuple[float, bool]:
    db_price = float(row["current_price"])
    return await get_live_yes_price(yes_token_id(row.get("clob_token_ids")), db_price)


async def run_d49_scan() -> dict[str, Any]:
    rows = await fetch(_CANDIDATE_MARKETS_SQL)
    if not rows:
        logger.info("d49_scan_no_candidates")
        return {"candidates": 0, "ladders": 0, "pairs_evaluated": 0, "signals": 0, "inserted": 0}

    rows_by_market: dict[str, dict[str, Any]] = {r["market_id"]: dict(r) for r in rows}

    n_live_priced = 0
    live_prices: dict[str, float] = {}
    is_live_by_market: dict[str, bool] = {}
    for market_id, r in rows_by_market.items():
        live_price, is_live = await _resolve_live_price(r)
        live_prices[market_id] = live_price
        is_live_by_market[market_id] = is_live
        if is_live:
            n_live_priced += 1

    candidates = [
        {
            "market_id": r["market_id"],
            "question": r.get("question") or "",
            "end_date": r["end_date"],
            "token_id": yes_token_id(r.get("clob_token_ids")),
        }
        for r in rows_by_market.values()
    ]
    ladders = build_ladders(candidates)

    repo = get_detector_signal_repository()
    now = datetime.now(UTC)

    n_pairs_evaluated = 0
    n_signals = 0
    n_inserted = 0

    for (entity, end_date, window_key), rungs in ladders.items():
        end_date_key = end_date.isoformat()
        horizon_sec = max(
            60, int((datetime.combine(end_date, datetime.min.time()).replace(tzinfo=UTC) - now).total_seconds())
        )
        for i in range(len(rungs)):
            for j in range(i + 1, len(rungs)):
                r1: ParsedStrike = rungs[i]
                r2: ParsedStrike = rungs[j]
                n_pairs_evaluated += 1

                row1 = rows_by_market.get(r1.market_id)
                row2 = rows_by_market.get(r2.market_id)
                if row1 is None or row2 is None:
                    continue

                leg_k1 = LadderLeg(
                    market_id=r1.market_id,
                    strike=r1.strike,
                    price=live_prices[r1.market_id],
                    category=row1.get("category"),
                    token_id=yes_token_id(row1.get("clob_token_ids")),
                )
                leg_k2 = LadderLeg(
                    market_id=r2.market_id,
                    strike=r2.strike,
                    price=live_prices[r2.market_id],
                    category=row2.get("category"),
                    token_id=no_token_id(row2.get("clob_token_ids")),
                )

                arb = d49_strike_ladder.build_arb(
                    entity=entity,
                    end_date_key=end_date_key,
                    window_key=window_key,
                    leg_k1=leg_k1,
                    leg_k2=leg_k2,
                    now=now,
                    horizon_sec=horizon_sec,
                )
                if arb is None:
                    continue

                sig_yes_k1, sig_no_k2 = arb
                pair_key = sig_yes_k1.evidence["pair_key"]

                # Книги best-effort в evidence — исполнимость на shadow не гейтит эмит.
                book_k1 = await fetch_live_book(leg_k1.token_id) if leg_k1.token_id else None
                book_k2 = await fetch_live_book(leg_k2.token_id) if leg_k2.token_id else None

                # Ноги резолвятся live-ценой независимо (см. _resolve_live_price) — одна
                # может быть свежим WS-тиком, другая протухшим db-снапшотом (15 мин), что
                # даёт ложное "нарушение" от рассинхрона, а не реальной монотонности.
                # is_live_k1/is_live_k2 в evidence обеих ног — чтобы shadow-аудит мог
                # отфильтровать такие пары до перевода в virtual/live.
                is_live_k1 = is_live_by_market.get(r1.market_id, False)
                is_live_k2 = is_live_by_market.get(r2.market_id, False)

                for sig, leg_name, book in (
                    (sig_yes_k1, "yes_low", book_k1),
                    (sig_no_k2, "no_high", book_k2),
                ):
                    evidence = dict(sig.evidence)
                    evidence["book"] = book  # None если книга недоступна
                    evidence["is_live_k1"] = is_live_k1
                    evidence["is_live_k2"] = is_live_k2
                    tx_hash = bucket_tx_hash(bucket_id=f"{pair_key}|{leg_name}", ts=now, namespace="d49")
                    evidence["tx_hash"] = tx_hash
                    final_sig = Signal(
                        detector=sig.detector,
                        market_id=sig.market_id,
                        side=sig.side,
                        generated_at=sig.generated_at,
                        confidence=sig.confidence,
                        expected_edge_pct=sig.expected_edge_pct,
                        fair_price=sig.fair_price,
                        current_price=sig.current_price,
                        horizon_sec=sig.horizon_sec,
                        token_id=sig.token_id,
                        event_id=sig.event_id,
                        evidence=evidence,
                    )
                    n_signals += 1
                    try:
                        if await repo.insert(final_sig):
                            n_inserted += 1
                    except Exception as exc:
                        logger.warning("d49_scan_insert_failed", error=str(exc), market=final_sig.market_id)

    logger.info(
        "d49_scan_completed",
        candidates=len(rows),
        ladders=len(ladders),
        pairs_evaluated=n_pairs_evaluated,
        signals=n_signals,
        inserted=n_inserted,
        live_priced=n_live_priced,
    )
    return {
        "candidates": len(rows),
        "ladders": len(ladders),
        "pairs_evaluated": n_pairs_evaluated,
        "signals": n_signals,
        "inserted": n_inserted,
        "live_priced": n_live_priced,
    }
