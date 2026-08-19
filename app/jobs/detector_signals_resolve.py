"""Detector signals resolve — раз в час заполняет outcome + realized_roi (gross+net).

Для каждого detector_signals где resolved=false:
  1. Если связанный market resolved=true → posчитать outcome (WIN/LOSS)
     по signal.side vs market.winner и realized_roi на $1 stake (paper-trade).
  2. Если signal старше N × horizon_sec и market не resolved:
     a. markets.current_price в резолюционном экстремуме (≤0.02 / ≥0.98) →
        proxy-исход WIN/LOSS + outcome_proxy=true. Исход фактически предрешён,
        UMA-финализация просто отстаёт в БД — без proxy эти терминальные исходы
        выпадали из promote-статистики как roi=0 (аудит 2026-07-05: 97% EXPIRED
        early-exit строк были в экстремуме, см.
        research/virtual-stage-audit-2026-07-05/evidence/07-counterfactual-hold.md).
     b. mid-zone → EXPIRED, ROI=0 (честно unknown).
  3. Reconcile: outcome_proxy-строки, чей market позже реально резолвнулся,
     перепроверяются по markets.winner — флип исправляется (лог
     proxy_outcome_flipped), флаг снимается.

Side-effect proxy-резолва (осознанный): v2_execution_resolve._settle_filled
settle-ит virtual-ордера по ds.outcome → virtual-позиции на предрешённых рынках
закрываются proxy-исходом, не висят до UMA (снимает давление на exposure cap
после exit-паритета #648). Live-ордера НЕ затронуты — они settle-ятся только
по markets.winner. Если reconcile позже флипнет сигнал, уже settled virtual-ордер
не пересчитывается (принято: order-level virtual P&L и так paper-условен,
promote-гейты кормятся signal-level полями, которые reconcile чинит).

Хранится 2 формы ROI:
  - `realized_roi`: gross — без учёта Polymarket fee. Используется ML training
     (ml_market_picker_fit binary mode) — backwards-compat.
  - `realized_roi_net`: **per-notional** net ROI после Polymarket fee.
     Canonical formula: ROI_net = net_pnl / notional × 100
     где notional = stake / entry = stake × (1/entry_price).
     Используется research/dashboard и новый ml_market_picker_ev_fit.

Формулы (на $1 paper-stake), side-aware:
    entry_mid  = current_price (YES) | 1 - current_price (NO)
    entry_fill = entry_mid + half_spread(category)     ← только для NET (см. ниже)
    payout     = 1/entry − 1
    notional   = stake / entry   (= 1/entry при stake=1)
    WIN gross:  payout × 100                    [%]      ← по МИДУ, backward-compat
    LOSS gross: −100
    net (WIN и LOSS): compute_net_pnl(...) / notional × 100   ← по FILL со спредом
        — net_pnl из SSoT pnl_formulas.compute_net_pnl (1× fee на stake, один
          entry-leg для обоих исходов). См. fee-convention fix 2026-06-04 ниже.

GROSS и NET намеренно считаются по РАЗНЫМ ценам:
    gross — по миду, потому что на этом target учатся ML L1/L2 binary (смена
            определения потребовала бы синхронного re-fit; gross и так «без
            издержек» по смыслу — это чистое направление);
    net   — по fill со спредом, потому что это ЭКОНОМИКА, вход промоут-гейта,
            и она обязана совпадать с тем, что видит сайзер перед сделкой.
"""

from __future__ import annotations

from typing import Any

import structlog

from app.core.slippage import get_spread_estimate
from app.services.database import execute, fetch
from app.services.jsonb_utils import parse_jsonb
from app.services.pnl_formulas import compute_net_pnl

logger = structlog.get_logger()

EXPIRE_HORIZON_MULTIPLIER = 6
# Per-detector overrides — детекторы с very-short horizon (D45 5-30s) теряют ВСЕ
# сигналы в EXPIRED при default 6× (=30-180s), потому что Polymarket помечает
# market.resolved через минуты после end_date. D45 имеет smaller-than-normal
# horizon, поэтому компенсируем большим multiplier.
#
# D3_cross_mkt_arb — обратный случай: HORIZON_SEC=6ч (d3_cross_mkt_arb.py:33), но
# d3_scan сканирует события с end_date до 30 дней вперёд (d3_scan.py:50). Default
# 6× даёt EXPIRE через 36ч — задолго до фактического резолва большинства событий.
# Аудит 2026-07-03 (research/fable-gates-audit-2026-07-03/): 1328/1678 (79%) D3
# сигналов истории EXPIRED с realized_roi=0, все promote/ramp/counterfactual
# метрики видели ~четверть исходов. 120× = 6ч×120 = 30д, покрывает полный scan-window.
#
# M2_liquidity_vacuum — тот же класс, что D3, но причина в семантике HORIZON_SEC.
# У M2 это не «время до резолва», а «сигнал живёт 60м для snapshot-writer covering»
# (m2_liquidity_vacuum.py:38) — measurement, не trading. Резолвер же читает
# horizon_sec как окно ожидания исхода, поэтому 6× = 6ч при медиане времени до
# end_date 162.5ч. Аудит 2026-08-04: 4610/6607 (69.8%) сигналов EXPIRED с roi=0,
# в окно 6ч попадали лишь 18.9% рынков. M2 идёт через WSS-хук без scan-window по
# end_date, поэтому потолок взят из фактического распределения: 4300× = 179д
# покрывает 91.4% (30д дало бы 64.7%, год — 96.5% ценой годового ожидания).
DETECTOR_HORIZON_MULTIPLIER: dict[str, int] = {
    "d45_chainlink_leadlag": 600,  # 5s×600=50min, 30s×600=5ч — хватит для market.resolved
    "D3_cross_mkt_arb": 120,  # 6ч×120=30д — покрывает d3_scan end_date window
    "M2_liquidity_vacuum": 4300,  # 1ч×4300=179д — p90 фактического времени до end_date
}
PAPER_STAKE_USD = 1.0
# 50K чтобы догонять D4 (~1500/час). При LIMIT=5000 накапливался долг 19K overdue D4.
RESOLVE_BATCH_LIMIT = 50000

# Резолюционный экстремум для proxy-исхода EXPIRED-сигналов. Пороги из аудита
# virtual-стейджа (evidence/07): цена YES ≥0.98 → yes фактически выиграл,
# ≤0.02 → no. Между ними — mid-zone, честный EXPIRED (ждём UMA).
PROXY_RESOLVE_EXTREME_LOW = 0.02
PROXY_RESOLVE_EXTREME_HIGH = 0.98

# Cap для realized_roi полей — БД схема имеет numeric(8,3) = max 99999.999.
# Без clip'а отдельные сигналы с entry≈0.0005 (D43_resolution_dispute YES@0.0005)
# дают ROI ≈ 199,900% и роняют ВЕСЬ batch с asyncpg.NumericValueOutOfRangeError.
# Это лечит symptom (потеря 1 сигнала), а не cause (детектор эмитит signals на
# рынках с insolvable entry прайсом) — см. также фильтр в D43.detect().
ROI_PCT_CAP = 99_999.0


def _signal_outcome(signal_side: str, winner: str | None) -> str | None:
    """Возвращает 'WIN' / 'LOSS' / None (если winner неизвестен/void)."""
    if winner is None:
        return None
    w = winner.strip().lower()
    if w in ("yes",):
        return "WIN" if signal_side == "YES" else "LOSS"
    if w in ("no",):
        return "WIN" if signal_side == "NO" else "LOSS"
    return None


def _proxy_winner(market_price: float | None) -> str | None:
    """Proxy-победитель по текущей рыночной YES-цене в экстремуме.

    ≥0.98 → 'yes', ≤0.02 → 'no', mid-zone или NULL → None (unknown).
    """
    if market_price is None:
        return None
    if market_price >= PROXY_RESOLVE_EXTREME_HIGH:
        return "yes"
    if market_price <= PROXY_RESOLVE_EXTREME_LOW:
        return "no"
    return None


def _entry_price_for_side(current_price: float, side: str) -> float:
    """Стоимость 1 share нашей стороны.

    `current_price` хранится как YES-сторона. Для NO-сделки платим 1 - current_price.
    """
    if side == "YES":
        return current_price
    return 1.0 - current_price


def _fill_price_for_side(current_price: float, side: str, category: str | None) -> float:
    """Цена входа с учётом half-spread — то, что реально платим за 1 share.

    `detector_signals.current_price` — это МИДПОЙНТ: WS пишет `(best_bid+best_ask)/2`
    (`polymarket_ws_service`, якорь `midpoint =`). Покупаем же по ask, то есть на
    half-spread хуже мида — и для YES, и для NO (спред симметричен относительно мида,
    NO-сторона стоит `1 - bid` = `(1 - mid) + half_spread`).

    Почему поправка здесь, а не в `pnl_formulas.compute_net_pnl`: та функция —
    общий SSoT и для РЕАЛЬНЫХ fill'ов (`execution_orders`), где спред уже сидит в
    цене исполнения. Вычесть его там значило бы посчитать спред дважды — ровно та
    ошибка, которую убрали 2026-07-16 (комментарий в `pnl_formulas`, якорь
    «Двойной slippage-счёт убран»). Здесь же на входе заведомо мид, поэтому
    поправка нужна именно на этом уровне.

    Не путать с `ev_sizer._cost_drag_pct`: там half-spread вычитается из ОЖИДАЕМОГО
    EV перед сделкой. Здесь — из ФАКТИЧЕСКОГО ROI после резолва. Это две разные
    стороны одной монеты, а не дубль: до фикса сайзер считал вход честно, а
    отчётность — по миду, и детекторы сравнивались по завышенной планке.
    """
    entry_mid = _entry_price_for_side(current_price, side)
    half_spread = get_spread_estimate(category or "other")
    # Клип к <1.0: у экстремально дорогих сигналов (entry≈0.99) спред не должен
    # уводить цену за доллар — share дороже $1 не бывает.
    return min(entry_mid + half_spread, 0.999)


def _realized_roi_pct(outcome: str, current_price: float, side: str) -> float:
    """ROI в % на $1 paper-stake (gross — без fee), side-aware.

    Сохраняем backward-compat: ML L1/L2 binary mode учится на этом target.

    Clip к ROI_PCT_CAP — БД schema realized_roi = numeric(8,3) max 99999.999.
    Без clip'а D43_resolution_dispute YES@0.0005 даёт ROI=199,900% → overflow,
    asyncpg.NumericValueOutOfRangeError роняет ВЕСЬ batch (включая нормальные
    signals). Inцидент 2026-05-19.
    """
    if outcome == "WIN":
        entry = _entry_price_for_side(current_price, side)
        if entry <= 0:
            return 0.0
        roi_pct = (1.0 / entry - 1.0) * 100.0
        return round(min(roi_pct, ROI_PCT_CAP), 3)
    if outcome == "LOSS":
        return -100.0
    return 0.0


def _realized_roi_net_pct(
    outcome: str,
    current_price: float,
    side: str,
    category: str | None,
    fee_schedule: dict | None = None,
) -> float:
    """ROI в % per-notional с учётом Polymarket fee, side-aware.

    Canonical formula: ROI_net = net_pnl / notional × 100
    где notional = stake / entry  (при stake=1: notional = 1/entry).

    net_pnl считается через SSoT `pnl_formulas.compute_net_pnl` (1× fee на stake
    для обоих WIN/LOSS — hold-to-resolution имеет только entry-ногу, без exit-сделки).

    Fee-convention fix 2026-06-04: раньше эта функция использовала 2×fee на
    gross_notional для WIN — ту самую формулу, которую settlement SSoT
    (`compute_net_pnl` / `_compute_settlement_pnl`) пофиксил 2026-05-18 как
    «inflated-fee bug» (завышало fee в 5×). Теперь обе конвенции совпадают, и
    realized_roi_net (вход промоут-гейта) отражает ту же экономику, что
    execution_orders.net_pnl.

    Half-spread fix 2026-08-05: раньше вход считался по МИДУ, хотя покупаем по ask.
    Из-за этого отчётность была системно оптимистичнее сайзера, который спред уже
    вычитал (`ev_sizer._cost_drag_pct`). Сдвиг ≈2.5 п.п. переворачивал вердикт:
    D38 из +0.27% превращается в −2.15%, D26 из +0.96% в −1.25%. Теперь вход —
    через `_fill_price_for_side`, там же объяснение, почему поправка здесь, а не
    в SSoT. Историю бэкфилит миграция `..._backfill_realized_roi_net_half_spread`.

    fee_schedule — опциональный per-market dict из markets.fee_schedule.
    """
    if outcome not in ("WIN", "LOSS"):
        return 0.0

    # Вырожденность проверяем по МИДУ, а не по цене со спредом. Иначе сигнал с
    # current_price=0 (шара по нулю не существует) даёт entry=half_spread и
    # ROI≈+97% — сломанный вход выглядит как почти бесплатный выигрыш.
    entry_mid = _entry_price_for_side(current_price, side)
    if entry_mid <= 0:
        return 0.0

    entry = _fill_price_for_side(current_price, side, category)
    if entry <= 0:
        return 0.0

    eff = max(entry, 0.001)
    notional = PAPER_STAKE_USD / eff  # = stake × (1/entry)
    # compute_net_pnl принимает YES-цену и сам делает side-aware конверсию, поэтому
    # цену со спредом возвращаем в YES-координаты: для NO вход (1-yes)+hs, значит
    # эквивалентная yes-цена = 1-entry.
    fill_yes_price = eff if side == "YES" else 1.0 - eff
    net_pnl = compute_net_pnl(
        outcome=outcome,
        side=side,
        market_yes_price=fill_yes_price,
        stake_usd=PAPER_STAKE_USD,
        category=category,
        fee_schedule=fee_schedule,
    )

    roi_pct = net_pnl / notional * 100.0
    # Clip к [-ROI_PCT_CAP, +ROI_PCT_CAP] — БД numeric(8,3) max 99999.999.
    # Net ROI per-notional обычно ≤100% (Polymarket payout = 1/p), но при entry≈0
    # extreme расчёты могут пробить cap. См. _realized_roi_pct.
    return round(max(min(roi_pct, ROI_PCT_CAP), -ROI_PCT_CAP), 3)


_FETCH_RESOLVABLE_SQL = """
WITH market_outcome AS (
    -- Финальный outcome — берём напрямую из markets.winner (Gamma resolved + outcomePrices fallback).
    -- 2026-05-13 ФИКС: раньше JOIN'или oracle_snapshots which is never populated в V2 эпохе.
    SELECT m.polymarket_id AS market_id,
           m.winner        AS winner
      FROM markets m
     WHERE m.resolved = true
       AND m.winner IN ('yes', 'no')
),
detector_multipliers AS (
    -- Per-detector override для horizon multiplier. NULL → default.
    SELECT unnest($3::text[]) AS detector,
           unnest($4::int[])  AS multiplier
)
SELECT ds.id,
       ds.detector,
       ds.market_id,
       ds.side,
       ds.current_price,
       ds.horizon_sec,
       ds.generated_at,
       (mo.winner IS NOT NULL) AS market_resolved,
       mo.winner               AS market_winner,
       m.category              AS category,
       m.fee_schedule          AS fee_schedule,
       m.current_price         AS market_price_now
  FROM detector_signals ds
  LEFT JOIN market_outcome mo      ON mo.market_id = ds.market_id
  LEFT JOIN markets m              ON m.polymarket_id = ds.market_id
  LEFT JOIN detector_multipliers dm ON dm.detector = ds.detector
 WHERE ds.resolved = false
   AND (
        mo.winner IS NOT NULL
        OR ds.generated_at < now() - make_interval(
            secs => ds.horizon_sec * COALESCE(dm.multiplier, $1)
        )
   )
 LIMIT $2
"""

_UPDATE_SQL = """
UPDATE detector_signals
   SET resolved          = true,
       outcome           = data.outcome,
       realized_roi      = data.realized_roi,
       realized_roi_net  = data.realized_roi_net,
       outcome_proxy     = data.outcome_proxy,
       resolved_at       = now()
  FROM (
    SELECT
      unnest($1::uuid[])      AS id,
      unnest($2::text[])      AS outcome,
      unnest($3::numeric[])   AS realized_roi,
      unnest($4::numeric[])   AS realized_roi_net,
      unnest($5::boolean[])   AS outcome_proxy
  ) AS data
 WHERE detector_signals.id = data.id
"""

# Reconcile: proxy-строки, чей market позже реально резолвнулся. Частичный
# индекс idx_detector_signals_outcome_proxy делает выборку дешёвой.
_FETCH_RECONCILABLE_SQL = """
SELECT ds.id,
       ds.detector,
       ds.side,
       ds.current_price,
       ds.outcome,
       m.winner         AS market_winner,
       m.category       AS category,
       m.fee_schedule   AS fee_schedule
  FROM detector_signals ds
  JOIN markets m ON m.polymarket_id = ds.market_id
 WHERE ds.outcome_proxy = true
   AND m.resolved = true
   AND m.winner IN ('yes', 'no')
 LIMIT $1
"""

_RECONCILE_UPDATE_SQL = """
UPDATE detector_signals
   SET outcome          = data.outcome,
       realized_roi     = data.realized_roi,
       realized_roi_net = data.realized_roi_net,
       outcome_proxy    = false
  FROM (
    SELECT
      unnest($1::uuid[])      AS id,
      unnest($2::text[])      AS outcome,
      unnest($3::numeric[])   AS realized_roi,
      unnest($4::numeric[])   AS realized_roi_net
  ) AS data
 WHERE detector_signals.id = data.id
"""


async def _reconcile_proxy_signals() -> dict[str, int]:
    """Перепроверить proxy-резолвнутые сигналы по реальному markets.winner.

    Подтверждённый исход → флаг снят (строка становится обычной terminal).
    Флип (UMA опроверг экстремум) → outcome/ROI исправлены, warning в лог —
    рост flip-rate означает, что пороги экстремума слишком мягкие.
    """
    rows = await fetch(_FETCH_RECONCILABLE_SQL, RESOLVE_BATCH_LIMIT)
    if not rows:
        return {"proxy_reconciled": 0, "proxy_flipped": 0}

    ids: list[Any] = []
    outcomes: list[str] = []
    rois: list[float] = []
    rois_net: list[float] = []
    flipped = 0

    for row in rows:
        side = row["side"]
        outcome = _signal_outcome(side, row["market_winner"])
        if outcome is None:
            continue
        current_price = float(row["current_price"])
        ids.append(row["id"])
        outcomes.append(outcome)
        rois.append(_realized_roi_pct(outcome, current_price, side))
        rois_net.append(
            _realized_roi_net_pct(
                outcome, current_price, side, row.get("category"), parse_jsonb(row.get("fee_schedule"))
            )
        )
        if outcome != row["outcome"]:
            flipped += 1
            logger.warning(
                "proxy_outcome_flipped",
                signal_id=str(row["id"]),
                detector=row.get("detector"),
                proxy_outcome=row["outcome"],
                market_outcome=outcome,
            )

    if ids:
        await execute(_RECONCILE_UPDATE_SQL, ids, outcomes, rois, rois_net)

    return {"proxy_reconciled": len(ids), "proxy_flipped": flipped}


async def run_detector_signals_resolve() -> dict[str, Any]:
    detector_keys = list(DETECTOR_HORIZON_MULTIPLIER.keys())
    detector_vals = [DETECTOR_HORIZON_MULTIPLIER[k] for k in detector_keys]
    rows = await fetch(
        _FETCH_RESOLVABLE_SQL,
        EXPIRE_HORIZON_MULTIPLIER,
        RESOLVE_BATCH_LIMIT,
        detector_keys,
        detector_vals,
    )

    ids: list[Any] = []
    outcomes: list[str] = []
    rois: list[float] = []
    rois_net: list[float] = []
    proxy_flags: list[bool] = []
    wins = losses = expired = proxy_wins = proxy_losses = 0

    for row in rows:
        side = row["side"]
        if row["market_resolved"]:
            winner = row["market_winner"]
            is_proxy = False
        else:
            # Таймаут. Цена в резолюционном экстремуме → proxy-победитель
            # (reconcile перепроверит по реальному winner, когда UMA догонит),
            # mid-zone/NULL → None → честный EXPIRED.
            market_price_now = row.get("market_price_now")
            winner = _proxy_winner(float(market_price_now) if market_price_now is not None else None)
            is_proxy = True

        outcome = _signal_outcome(side, winner)
        if outcome is not None:
            current_price = float(row["current_price"])
            roi = _realized_roi_pct(outcome, current_price, side)
            roi_net = _realized_roi_net_pct(
                outcome, current_price, side, row.get("category"), parse_jsonb(row.get("fee_schedule"))
            )
            ids.append(row["id"])
            outcomes.append(outcome)
            rois.append(roi)
            rois_net.append(roi_net)
            proxy_flags.append(is_proxy)
            if is_proxy and outcome == "WIN":
                proxy_wins += 1
            elif is_proxy:
                proxy_losses += 1
            elif outcome == "WIN":
                wins += 1
            else:
                losses += 1
        elif row["market_resolved"]:
            # winner void/неизвестен несмотря на market_resolved — не пишем,
            # строка попадёт в следующий прогон (existing behavior)
            continue
        else:
            ids.append(row["id"])
            outcomes.append("EXPIRED")
            rois.append(0.0)
            rois_net.append(0.0)
            proxy_flags.append(False)
            expired += 1

    if ids:
        await execute(_UPDATE_SQL, ids, outcomes, rois, rois_net, proxy_flags)

    reconcile_stats = await _reconcile_proxy_signals()

    logger.info(
        "detector_signals_resolved",
        checked=len(rows),
        wins=wins,
        losses=losses,
        expired=expired,
        proxy_wins=proxy_wins,
        proxy_losses=proxy_losses,
        **reconcile_stats,
    )
    return {
        "checked": len(rows),
        "wins": wins,
        "losses": losses,
        "expired": expired,
        "proxy_wins": proxy_wins,
        "proxy_losses": proxy_losses,
        **reconcile_stats,
    }
