"""Shadow TP/SL simulation поверх price snapshots.

Идея (PR-B из shadow stage переделки, см.
research/shadow-gates-methodology-2026-05/2026-05-27_validation.md):

shadow `realized_roi_net` считает PnL **hold-to-resolution** — что произошло
когда рынок резолвнулся. Это **не та** экономика, которую видит virtual exec:
virtual закрывает позиции по TP/SL раньше резолва (если они срабатывают).

Поэтому shadow gates на основе hold-to-resolution PnL **систематически
mispredict** virtual PnL — особенно для ML детекторов где TP fires быстро на
small moves (STANDALONE shadow −3% vs virtual +58%).

Решение: для каждого signal'а replay actual price path (из
detector_signal_price_snapshots), применить per-detector TP/SL params
(из position_manager._DETECTOR_OVERRIDES), и вычислить **simulated net PnL**
как если бы мы реально торговали этот signal в virtual exec.

Формула:
  entry = signal.current_price (или 1 - current_price для NO side)
  payout = 1 / entry
  на каждом snapshot:
    pnl_pct = (current_payout - entry_payout) / entry_payout × 100
    if pnl_pct >= tp_pct: exit_reason = take_profit, sim_pnl = +tp_pct
    if pnl_pct <= sl_pct: exit_reason = stop_loss, sim_pnl = +sl_pct (it's negative)
  if никакой trigger не сработал и signal RESOLVED:
    exit_reason = resolution
    sim_pnl = realized_roi_net  (hold-to-resolution; уже в процентах)
  if EXPIRED:
    exit_reason = expired
    sim_pnl = 0

Fees: вычитаем 2× single-trade fee (entry + exit) из sim_pnl.

Не зависит от: virtual_execution, position_manager runtime. Pure function
+ DB read из price snapshots. Тестируемо.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from app.jobs.position_manager import (
    HOLD_TO_RESOLUTION_DETECTORS,
    _thresholds_for,
)

# Polymarket fee: 2% на entry maker, 2% на exit maker. На taker — то же.
# Для shadow simulation берём conservative 2% per side = 4% round-trip.
# Это уже учтено в realized_roi_net для hold-to-resolution; в TP/SL exits
# применяем дополнительно к pnl_pct.
ROUND_TRIP_FEE_PCT = 4.0


def _tp_sl_for(detector: str) -> tuple[float, float]:
    """Per-detector TP/SL thresholds (reuse position_manager logic).

    Делегирует в position_manager._thresholds_for — единственный источник правды
    для (tp_pct, sl_pct), устойчивый к расширению _DETECTOR_OVERRIDES до троек
    (max_hold_hours добавлен 2026-07-02, audit топ-1 live exit path).
    """
    return _thresholds_for(detector)


@dataclass(frozen=True)
class ShadowSimResult:
    """Результат симуляции одного signal'а.

    sim_pnl_pct — это **net** PnL % (после fees). Положительное = profit.

    exit_reason values:
      - 'take_profit' — TP threshold достигнут на каком-то snapshot
      - 'stop_loss'   — SL threshold достигнут
      - 'resolution'  — signal дошёл до resolved=true без TP/SL trigger
      - 'expired'     — signal истёк (outcome=EXPIRED), realized_roi=0
      - 'no_data'     — нет snapshots И signal ещё не resolved → не считаем
    """

    detector: str
    signal_id: str
    entry_yes_price: float
    side: str  # 'YES' | 'NO'
    tp_pct: float
    sl_pct: float
    exit_reason: str
    sim_pnl_pct: float  # net % (после fees)
    n_snapshots: int
    exit_yes_price: float | None
    is_hold_to_resolution: bool


def _signal_payout_pct_at(
    entry_yes: float,
    current_yes: float,
    side: str,
) -> float:
    """Return PnL % при entry @ entry_yes и текущей цене current_yes.

    Для NO side: cost = 1 - entry_yes, current_cost = 1 - current_yes.
    Для YES side: cost = entry_yes, current_cost = current_yes.

    PnL% = (current_cost - cost) / cost × 100.
    """
    if side.upper() == "NO":
        entry_cost = 1.0 - entry_yes
        current_cost = 1.0 - current_yes
    else:  # YES
        entry_cost = entry_yes
        current_cost = current_yes
    if entry_cost <= 0:
        return 0.0
    return (current_cost - entry_cost) / entry_cost * 100.0


def simulate_signal(
    *,
    detector: str,
    signal_id: str,
    side: str,
    entry_yes_price: float,
    price_path: list[dict[str, Any]],
    resolved: bool,
    outcome: str | None,
    realized_roi_net: float | None,
) -> ShadowSimResult:
    """Симулирует TP/SL execution для signal'а.

    Args:
        detector: detector name (для lookup TP/SL params)
        signal_id: detector_signal.id
        side: 'YES' | 'NO'
        entry_yes_price: signal.current_price (yes price at signal time)
        price_path: list of {captured_at, yes_price} from
                    fetch_signal_price_path()
        resolved: detector_signal.resolved
        outcome: detector_signal.outcome (WIN/LOSS/EXPIRED/None)
        realized_roi_net: hold-to-resolution net ROI в ПРОЦЕНТАХ (e.g. 5.0 = +5%,
            как хранится в detector_signals.realized_roi_net — net_pnl/notional*100)

    Returns: ShadowSimResult.
    """
    tp_pct, sl_pct = _tp_sl_for(detector)
    is_h2r = detector in HOLD_TO_RESOLUTION_DETECTORS

    # Walk price path checking TP/SL triggers.
    # Для HOLD_TO_RESOLUTION детекторов skip TP/SL check — всегда edge
    # реализуется at resolution.
    if not is_h2r:
        for snap in price_path:
            current_yes = float(snap["yes_price"])
            pnl_pct = _signal_payout_pct_at(entry_yes_price, current_yes, side)
            if pnl_pct >= tp_pct:
                # TP fired
                return ShadowSimResult(
                    detector=detector,
                    signal_id=signal_id,
                    entry_yes_price=entry_yes_price,
                    side=side,
                    tp_pct=tp_pct,
                    sl_pct=sl_pct,
                    exit_reason="take_profit",
                    sim_pnl_pct=tp_pct - ROUND_TRIP_FEE_PCT,
                    n_snapshots=len(price_path),
                    exit_yes_price=current_yes,
                    is_hold_to_resolution=False,
                )
            if pnl_pct <= sl_pct:
                # SL fired
                return ShadowSimResult(
                    detector=detector,
                    signal_id=signal_id,
                    entry_yes_price=entry_yes_price,
                    side=side,
                    tp_pct=tp_pct,
                    sl_pct=sl_pct,
                    exit_reason="stop_loss",
                    sim_pnl_pct=sl_pct - ROUND_TRIP_FEE_PCT,
                    n_snapshots=len(price_path),
                    exit_yes_price=current_yes,
                    is_hold_to_resolution=False,
                )

    # No TP/SL trigger (или hold-to-resolution detector).
    if not resolved:
        # Signal ещё активен, нечего вернуть.
        # Эта ситуация для open positions — caller должен skip их в aggregation.
        return ShadowSimResult(
            detector=detector,
            signal_id=signal_id,
            entry_yes_price=entry_yes_price,
            side=side,
            tp_pct=tp_pct,
            sl_pct=sl_pct,
            exit_reason="no_data",
            sim_pnl_pct=0.0,
            n_snapshots=len(price_path),
            exit_yes_price=None,
            is_hold_to_resolution=is_h2r,
        )

    if outcome == "EXPIRED":
        return ShadowSimResult(
            detector=detector,
            signal_id=signal_id,
            entry_yes_price=entry_yes_price,
            side=side,
            tp_pct=tp_pct,
            sl_pct=sl_pct,
            exit_reason="expired",
            sim_pnl_pct=0.0,
            n_snapshots=len(price_path),
            exit_yes_price=None,
            is_hold_to_resolution=is_h2r,
        )

    # Hold-to-resolution path: use realized_roi_net as ground truth.
    # realized_roi_net уже в ПРОЦЕНТАХ (net_pnl/notional*100, см.
    # detector_signals_resolve._realized_roi_net_pct). НЕ домножать на 100 —
    # это завышало hold-to-resolution PnL в 100× (fix 2026-05-29).
    pnl = float(realized_roi_net or 0)
    return ShadowSimResult(
        detector=detector,
        signal_id=signal_id,
        entry_yes_price=entry_yes_price,
        side=side,
        tp_pct=tp_pct,
        sl_pct=sl_pct,
        exit_reason="resolution",
        sim_pnl_pct=pnl,  # realized_roi_net (в %) уже включает fees+slippage
        n_snapshots=len(price_path),
        exit_yes_price=None,
        is_hold_to_resolution=is_h2r,
    )


# ─── Aggregation helpers — для compute_promotion_gates (PR-C) ─────────────────


def aggregate_sim_results(results: list[ShadowSimResult]) -> dict[str, Any]:
    """Aggregate ShadowSimResult list → summary stats per detector.

    Returns: {n_total, n_tp, n_sl, n_resolution, n_expired, n_no_data,
              sum_pnl_pct, mean_pnl_pct, tp_rate_pct, sl_rate_pct}.
    """
    if not results:
        return {
            "n_total": 0,
            "n_tp": 0,
            "n_sl": 0,
            "n_resolution": 0,
            "n_expired": 0,
            "n_no_data": 0,
            "sum_pnl_pct": 0.0,
            "mean_pnl_pct": None,
            "tp_rate_pct": None,
            "sl_rate_pct": None,
        }
    n_total = len(results)
    n_tp = sum(1 for r in results if r.exit_reason == "take_profit")
    n_sl = sum(1 for r in results if r.exit_reason == "stop_loss")
    n_resolution = sum(1 for r in results if r.exit_reason == "resolution")
    n_expired = sum(1 for r in results if r.exit_reason == "expired")
    n_no_data = sum(1 for r in results if r.exit_reason == "no_data")

    # PnL агрегаты только по closed (не no_data — там нет valid PnL).
    closed = [r for r in results if r.exit_reason != "no_data"]
    sum_pnl = sum(r.sim_pnl_pct for r in closed)
    mean_pnl = sum_pnl / len(closed) if closed else None

    # TP/SL rates от total closed (не от total — иначе no_data занижает).
    n_closed = len(closed)
    tp_rate = round(100.0 * n_tp / n_closed, 1) if n_closed > 0 else None
    sl_rate = round(100.0 * n_sl / n_closed, 1) if n_closed > 0 else None

    return {
        "n_total": n_total,
        "n_tp": n_tp,
        "n_sl": n_sl,
        "n_resolution": n_resolution,
        "n_expired": n_expired,
        "n_no_data": n_no_data,
        "sum_pnl_pct": round(sum_pnl, 1),
        "mean_pnl_pct": round(mean_pnl, 2) if mean_pnl is not None else None,
        "tp_rate_pct": tp_rate,
        "sl_rate_pct": sl_rate,
    }
