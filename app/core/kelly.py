"""Kelly criterion — fraction sizing and bankroll simulation."""

_MIN_STAKE = 5.0
_MAX_HALF_KELLY = 0.25


def kelly_fraction(win_rate: float, avg_win: float, avg_loss: float) -> float:
    """f* = (p * b - q) / b, where b = avg_win/avg_loss. Capped at 25%."""
    if avg_loss == 0 or win_rate <= 0 or win_rate >= 1:
        return 0.0
    b = abs(avg_win / avg_loss)
    q = 1.0 - win_rate
    f = (win_rate * b - q) / b
    return max(0.0, min(f, 0.25))


def _safe_float(val, default: float = 0.0) -> float:
    """Безопасная конвертация asyncpg-значений в float."""
    if val is None:
        return default
    try:
        return float(val)
    except (TypeError, ValueError):
        return default


def run_kelly_simulation(trades: list[dict], bankroll: float) -> tuple[list[dict], list[dict], dict]:
    """Последовательная Half-Kelly симуляция.

    Args:
        trades: список dict с ключами: pnl_per_unit, outcome, market_price,
                created_at, question, category, whale_contribution_raw, market_id
        bankroll: начальный банкролл

    Returns:
        (enriched_trades, equity_curve, summary)
    """
    current_bankroll = bankroll
    peak = bankroll
    max_drawdown = 0.0
    wins = 0
    losses = 0
    total_pnl = 0.0

    enriched: list[dict] = []
    equity_curve: list[dict] = []

    for t in trades:
        pnl_per_unit = _safe_float(t.get("pnl_per_unit"))
        is_win = t.get("prediction_correct", t.get("outcome") == "yes")

        # Rolling stats для Kelly
        total_trades = wins + losses
        rolling_wr = wins / total_trades if total_trades > 0 else 0.5

        # Half-Kelly
        half_kelly = kelly_fraction(rolling_wr, 0.3, 0.85) * 0.5
        stake = min(
            max(current_bankroll * half_kelly, _MIN_STAKE),
            current_bankroll * _MAX_HALF_KELLY,
        )

        if stake > current_bankroll:
            stake = current_bankroll
        if current_bankroll <= 0:
            break

        pnl = pnl_per_unit * stake
        current_bankroll += pnl
        total_pnl += pnl

        if is_win:
            wins += 1
        else:
            losses += 1

        if current_bankroll > peak:
            peak = current_bankroll
        dd = (peak - current_bankroll) / peak if peak > 0 else 0.0
        if dd > max_drawdown:
            max_drawdown = dd

        created_at = t.get("created_at")
        trade_date = str(created_at)[:10] if created_at else None

        enriched.append(
            {
                "date": trade_date,
                "market_id": t.get("market_id"),
                "question": t.get("question", ""),
                "category": t.get("category", ""),
                "market_price": _safe_float(t.get("market_price")),
                "outcome": t.get("outcome", ""),
                "kelly_fraction": round(half_kelly, 4),
                "stake": round(stake, 2),
                "pnl": round(pnl, 2),
                "bankroll_after": round(current_bankroll, 2),
            }
        )

        equity_curve.append({"date": trade_date, "bankroll": round(current_bankroll, 2)})

    total_trades = wins + losses
    win_rate = wins / total_trades if total_trades > 0 else 0.0

    summary = {
        "initial_bankroll": bankroll,
        "final_bankroll": round(current_bankroll, 2),
        "total_pnl": round(total_pnl, 2),
        "roi_pct": round((total_pnl / bankroll) * 100, 2) if bankroll > 0 else 0.0,
        "total_trades": total_trades,
        "wins": wins,
        "losses": losses,
        "win_rate": round(win_rate * 100, 2),
        "max_drawdown_pct": round(max_drawdown * 100, 2),
        "current_kelly": round(kelly_fraction(win_rate, 0.3, 0.85) * 0.5, 4),
    }

    return enriched, equity_curve, summary


def compute_temporal_buckets(trades: list[dict]) -> list[dict]:
    """Группировка enriched trades по days_to_resolve бакетам."""
    buckets: dict[str, dict] = {}

    for t in trades:
        days = _safe_float(t.get("days_to_resolve"))
        if days < 1:
            key = "<24h"
        elif days < 3:
            key = "1-3d"
        elif days < 7:
            key = "3-7d"
        elif days < 14:
            key = "7-14d"
        else:
            key = ">14d"

        if key not in buckets:
            buckets[key] = {"wins": 0, "total": 0, "pnl_sum": 0.0}

        b = buckets[key]
        b["total"] += 1
        if t.get("prediction_correct", False):
            b["wins"] += 1
        b["pnl_sum"] += _safe_float(t.get("pnl"))

    order = ["<24h", "1-3d", "3-7d", "7-14d", ">14d"]
    result = []
    for key in order:
        if key not in buckets:
            continue
        b = buckets[key]
        total = b["total"]
        result.append(
            {
                "bucket": key,
                "trades": total,
                "win_rate": round((b["wins"] / total) * 100, 2) if total > 0 else 0.0,
                "pnl_sum": round(b["pnl_sum"], 2),
                "avg_pnl": round(b["pnl_sum"] / total, 4) if total > 0 else 0.0,
            }
        )
    return result
