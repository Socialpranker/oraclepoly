"""D49 StrikeLadder — model-free арбитраж на монотонности крипто-лесенок.

Вердикт бэктеста: `research/d49-strike-ladder-2026-07-09/answer.md` (KEEP →
shadow-scanner, НЕ live/virtual). 1.03 net-профитных сигнала/день на живом
периоде (2025-01 → 2026-04), медианный net-спред +3.88pp после fee+slippage.
Главный риск — snapshot-цена ≠ гарантированно исполнимая live-цена
(тот же caveat, что и temperature lock-in), поэтому shadow-режим минимум
4-6 недель перед virtual/live.

Гипотеза: для рынков "<ASSET> above $K by <date>" одного expiry обязано
P(YES > K1) >= P(YES > K2) при K1 < K2 (более высокий страйк не может быть
вероятнее более низкого). Нарушение p(K2) - p(K1) > buffer = арбитраж:
BUY YES(K1) @ p1 + BUY NO(K2) @ (1-p2), суммарная стоимость p1 + (1-p2) < 1
гарантированно при p2 > p1 → payoff >= $1 при ЛЮБОМ исходе.

Non-overlap с D3 (verified в бэктесте, шаг 0): все крипто strike-рынки в БД
имеют neg_risk=false — D3 сканирует только neg_risk=true AND neg_risk_other=false
(app/jobs/d3_scan.py). D49 работает в непересекающемся скоупе.

Пара эмитит ДВА сигнала (не один): YES на K1-ногу + NO на K2-ногу — обе ноги
нужно исполнить, чтобы реализовать арбитраж. Оба сигнала несут общий pair_key
в evidence для последующей склейки/анализа.

confidence — переменный (урок аудита virtual-patterns: константный confidence
непроверяем), масштабируется от net_spread_pp:
    confidence = min(0.95, 0.55 + net_spread_pp * 0.03)
Обоснование: 0.55 — минимальный порог выше random (сигнал уже прошёл buffer-гейт,
т.е. заведомо не шум), +0.03 confidence на каждый pp net-спреда сверх buffer
(при net_spread_pp≈13.3pp confidence достигает cap 0.95). Не откалибровано —
shadow-стадия должна накопить достаточно резолвов для L1 Beta calibrator.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from app.detectors.contract import Signal
from app.services.fee_calculator import estimate_trading_fee

DETECTOR_NAME = "D49_strike_ladder"

# Slippage haircut per leg (roadmap-дефолт из бэктеста, см. answer.md §Метод).
SLIPPAGE_PP_PER_LEG = 1.5

# Buffer = fee обеих ног + 2×slippage. Динамический (зависит от category/price),
# считается в build_arb() через estimate_trading_fee, здесь только slippage-часть.
_SLIPPAGE_BUFFER_PP = SLIPPAGE_PP_PER_LEG * 2

# Confidence formula constants (см. docstring модуля).
_CONFIDENCE_BASE = 0.55
_CONFIDENCE_SLOPE = 0.03
_CONFIDENCE_CAP = 0.95

# Signal horizon — до end_date лесенки, capped чтобы не эмитить месячные горизонты
# на близких resolution (near-term arb закрывается быстро).
_MAX_HORIZON_SEC = 7 * 24 * 60 * 60


@dataclass(frozen=True)
class LadderLeg:
    """Одна нога лесенки — минимальный набор полей для detect()."""

    market_id: str  # markets.polymarket_id
    strike: float
    price: float  # текущая YES-цена (live-override уже применён снаружи)
    category: str | None
    token_id: str | None  # yes_token_id или no_token_id, в зависимости от leg


def _confidence_from_spread(net_spread_pp: float) -> float:
    """confidence = min(cap, base + net_spread_pp * slope). См. docstring модуля."""
    return round(min(_CONFIDENCE_CAP, _CONFIDENCE_BASE + net_spread_pp * _CONFIDENCE_SLOPE), 4)


def build_arb(
    *,
    entity: str,
    end_date_key: str,
    window_key: str,
    leg_k1: LadderLeg,
    leg_k2: LadderLeg,
    now: datetime | None = None,
    horizon_sec: int | None = None,
) -> tuple[Signal, Signal] | None:
    """Строит пару сигналов (YES на K1, NO на K2) если нарушение монотонности
    net-профитно после fee+slippage. Возвращает None если buffer не пройден.

    leg_k1.strike < leg_k2.strike обязательно (вызывающий гарантирует порядок —
    build_ladders() уже сортирует rungs по strike).
    """
    if leg_k1.strike >= leg_k2.strike:
        return None

    p1 = leg_k1.price
    p2 = leg_k2.price
    if not (0.0 < p1 < 1.0 and 0.0 < p2 < 1.0):
        return None

    category = leg_k1.category or leg_k2.category or "other"
    fee_leg1 = estimate_trading_fee(1.0, p1, category)  # BUY YES(K1) @ p1
    fee_leg2 = estimate_trading_fee(1.0, 1.0 - p2, category)  # BUY NO(K2) @ (1-p2)
    fees_total_pp = (fee_leg1 + fee_leg2) * 100.0
    buffer_pp = fees_total_pp + _SLIPPAGE_BUFFER_PP

    net_spread_pp = (p2 - p1) * 100.0  # positive = нарушение монотонности (арбитраж)
    net_edge_after_fees_pp = net_spread_pp - buffer_pp
    if net_edge_after_fees_pp <= 0:
        return None

    now = now or datetime.now(UTC)
    confidence = _confidence_from_spread(net_edge_after_fees_pp)
    pair_key = f"{entity}:{end_date_key}:{window_key}:{leg_k1.strike:g}:{leg_k2.strike:g}"

    shared_evidence: dict[str, Any] = {
        "pair_key": pair_key,
        "entity": entity,
        "end_date": end_date_key,
        "window_key": window_key,
        "k1": leg_k1.strike,
        "k2": leg_k2.strike,
        "p1": p1,
        "p2": p2,
        "net_spread_pp": round(net_spread_pp, 4),
        "buffer_pp": round(buffer_pp, 4),
        "fee_leg1_pp": round(fee_leg1 * 100.0, 4),
        "fee_leg2_pp": round(fee_leg2 * 100.0, 4),
    }

    signal_horizon = horizon_sec if horizon_sec is not None else _MAX_HORIZON_SEC

    # Нога 1: BUY YES(K1) @ p1. fair_price = p2 — минимум монотонного восстановления
    # (если p1 подтянется хотя бы до p2, монотонность восстановлена).
    sig_yes_k1 = Signal(
        detector=DETECTOR_NAME,
        market_id=leg_k1.market_id,
        side="YES",
        generated_at=now,
        confidence=confidence,
        expected_edge_pct=round(net_edge_after_fees_pp, 4),
        fair_price=round(p2, 4),
        current_price=round(p1, 4),
        horizon_sec=signal_horizon,
        token_id=leg_k1.token_id,
        evidence={**shared_evidence, "leg": "yes_low"},
    )

    # Нога 2: BUY NO(K2) @ (1-p2). Contract: current_price = YES price (canonical);
    # resolve сам считает NO entry как 1-current_price (app/jobs/detector_signals_resolve.py
    # _entry_price_for_side). Здесь current_price = p2 (сырая YES-цена K2-рынка), НЕ 1-p2 —
    # тот же контракт, что и во всех остальных NO-side детекторах (d3/d26/d29/d38): двойная
    # инверсия current_price=1-p2 давала бы resolve entry=1-(1-p2)=p2 вместо правильного 1-p2,
    # искажая realized_roi (~5.4× на примере p1=0.10/p2=0.30). fair_price тоже в YES-номинале =
    # p1 — минимум монотонного восстановления (YES-цена K2 должна опуститься до <= p1).
    sig_no_k2 = Signal(
        detector=DETECTOR_NAME,
        market_id=leg_k2.market_id,
        side="NO",
        generated_at=now,
        confidence=confidence,
        expected_edge_pct=round(net_edge_after_fees_pp, 4),
        fair_price=round(p1, 4),
        current_price=round(p2, 4),
        horizon_sec=signal_horizon,
        token_id=leg_k2.token_id,
        evidence={**shared_evidence, "leg": "no_high"},
    )

    return sig_yes_k1, sig_no_k2
