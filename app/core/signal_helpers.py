# app/core/signal_helpers.py
"""Shared helpers для signal pipeline — dedup, EV, horizon, signal dict.

Устраняет дублирование между oracle, contra_bias, market_baseline генераторами.
"""

from __future__ import annotations

from datetime import datetime

from app.services.fee_calculator import estimate_trading_fee


def dedup_by_event(items: list[dict], key_fn: callable) -> list[dict]:
    """Дедупликация маркетов по event_id — оставляет лучший по key_fn.

    Items без event_id проходят без фильтрации.
    """
    event_groups: dict[str, list[dict]] = {}
    no_event: list[dict] = []

    for item in items:
        eid = item.get("event_id")
        if eid:
            event_groups.setdefault(str(eid), []).append(item)
        else:
            no_event.append(item)

    result = list(no_event)
    for group in event_groups.values():
        result.append(max(group, key=key_fn))

    return result


def compute_ev(
    p_win: float,
    entry_price: float,
    stake: float,
    category: str,
    market_price: float | None = None,
    fee_schedule: dict | None = None,
) -> dict:
    """Рассчитать EV с учётом комиссии.

    fee_schedule — опциональный per-market dict из markets.fee_schedule
    (Gamma feeSchedule format). Если задан — точный rate, иначе fallback
    на категорийную таблицу в fee_calculator.

    Returns dict: gross_ev, net_ev, est_fee, p_win, payout_if_win, ev_percent.
    """
    entry = max(entry_price, 0.01)
    payout_if_win = stake * (1.0 / entry - 1.0)
    gross_notional = stake / entry
    mp = market_price if market_price is not None else entry
    est_fee = estimate_trading_fee(gross_notional, mp, category, fee_schedule)
    gross_ev = p_win * payout_if_win - (1.0 - p_win) * stake
    net_ev = gross_ev - est_fee

    return {
        "gross_ev": round(gross_ev, 4),
        "net_ev": round(net_ev, 2),
        "est_fee": round(est_fee, 4),
        "p_win": round(p_win, 4),
        "payout_if_win": round(payout_if_win, 2),
        "ev_percent": round(net_ev / stake * 100, 1) if stake > 0 else 0.0,
    }


def parse_horizon_days(end_date: str | datetime | None, created_at: str | datetime | None) -> float | None:
    """Парсинг горизонта в днях из end_date и created_at.

    Возвращает float >= 0.01 или None если данные отсутствуют/невалидны.
    """
    if end_date is None or created_at is None:
        return None

    end = _parse_dt(end_date)
    start = _parse_dt(created_at)

    if end is None or start is None:
        return None

    return max((end - start).total_seconds() / 86400, 0.01)


def _parse_dt(value: str | datetime | None) -> datetime | None:
    """Парсинг datetime из строки или datetime объекта."""
    if value is None:
        return None
    if isinstance(value, datetime):
        return value
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except (ValueError, TypeError):
        return None


def build_signal_dict(
    market: dict,
    side: str,
    entry_price: float,
    market_price: float,
    ev_result: dict,
    stake: float,
    signal_source: str,
    extra: dict | None = None,
) -> dict:
    """Построить стандартный signal dict.

    Собирает все поля из market, ev_result и параметров в единый формат.
    """
    signal = {
        # Market fields
        "market_id": str(market.get("market_id") or market.get("id", "")),
        "event_id": str(market["event_id"]) if market.get("event_id") else None,
        "question": (market.get("question") or "")[:120],
        "category": market.get("category") or "Other",
        "volume_24h": float(market.get("volume_24h") or 0),
        "end_date": market.get("end_date"),
        "whale_tx_count": market.get("whale_tx_count") or 0,
        "whale_contribution_raw": float(market.get("whale_contribution_raw") or 0.0),
        "z_posterior": float(market.get("z_posterior") or 0.0),
        "sm_registry_raw": float(market.get("sm_registry_raw") or 0.0),
        "micro_factor_raw": float(market.get("micro_factor_raw") or 0.0),
        "maker_direction_raw": (
            float(market["maker_direction_raw"]) if market.get("maker_direction_raw") is not None else None
        ),
        "taker_imbalance_raw": (
            float(market["taker_imbalance_raw"]) if market.get("taker_imbalance_raw") is not None else None
        ),
        "price_avg_7d": (float(market["price_avg_7d"]) if market.get("price_avg_7d") is not None else None),
        "news_contribution_raw": float(market.get("news_contribution_raw") or 0.0),
        "snapshot_created_at": market.get("created_at", ""),
        # Signal params
        "side": side,
        "market_price": round(market_price, 4),
        "entry_price": round(entry_price, 4),
        "stake": stake,
        "signal_source": signal_source,
        # EV result
        "ev": ev_result["net_ev"],
        "ev_percent": ev_result["ev_percent"],
        "est_fee": ev_result["est_fee"],
        "p_win": ev_result["p_win"],
        "payout_if_win": ev_result["payout_if_win"],
    }

    if extra:
        signal.update(extra)

    return signal
