"""Position Manager — Active TP/SL для virtual_filled позиций.

Запускается каждые 5 минут. Для каждой открытой virtual_filled позиции:
  1. Загружаем current_price из markets (market_sync раз в 15 мин из Gamma;
     realtime-цен здесь нет — см. research/design-audit-2026-07-02.md топ-3).
  2. Считаем unrealized PnL% от fill_price:
       - YES: (current / fill − 1) × 100
       - NO:  ((1−current) / (1−fill) − 1) × 100
  3. Если PnL% >= TP_THRESHOLD → закрываем как take_profit.
  4. Если PnL% <= SL_THRESHOLD → закрываем как stop_loss.
  5. Запись: status='virtual_settled', outcome='EARLY_EXIT',
     pnl = stake × (price_ratio − 1), net_pnl с учётом fee×2 + slippage×2.

Backtest evidence (2026-05-13):
  - D_meta_consensus: 8 идентичных NO@0.07 ушли в drawdown −91% (current 0.92).
    SL @ −30% спас бы ~$250 (закрытие при NO@0.043 вместо NO@~0.001).
  - STANDALONE_V2_LGB: 4 позиции уже на +20-37%. TP @ +30% реализовал бы прибыль
    до возврата к break-even (часто бывает на news whipsaws).

Параметры (per-detector override через portfolios JSON future work):
  TP_THRESHOLD_PCT = +50   — берём profit при удвоении (наблюдаемый pattern на 144 open)
  SL_THRESHOLD_PCT = −30   — режем losses до catastrophic level (был бы critical для D_meta)

NOT promoted detectors (paused) пропускаются — там signals не приходят.

Skip rules:
  - Markets с resolved=true (settle_filled закроет нормально).
  - Markets без current_price (нечего сравнивать).
  - Позиции открытые < 10 минут (не дёргаемся на entry noise).

Только virtual mode. Live execution — отдельный path после live promote.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

import structlog

from app.analytics.notification import Notification, Severity
from app.core.slippage import get_spread_estimate
from app.services.database import execute, fetch
from app.services.fee_calculator import estimate_trading_fee
from app.services.jsonb_utils import parse_jsonb
from app.services.notification_bus import publish

logger = structlog.get_logger(__name__)


# --- Configuration ---
# Default thresholds — могут быть переопределены per-detector через _DETECTOR_OVERRIDES.
# User decision 2026-05-13: фокус на mid-zone [0.50-0.70] predictions + high-edge везде.
# В mid-zone "double" (price 0.55 → 0.75) = +36%. Поэтому TP более скромный.
DEFAULT_TP_PCT: float = 30.0  # mid-zone realistic gain
DEFAULT_SL_PCT: float = -20.0  # mid-zone realistic loss (volatility ниже чем у extremes)

# Дополнительный exit gate: если цена ушла из mid-zone (>= 0.85 или <= 0.30),
# закрыть позицию вне зависимости от PnL. Это user constraint — "не интересны 99".
MID_ZONE_FLOOR: float = 0.30
MID_ZONE_CEILING: float = 0.85

# TP-реализм guard (2026-06-05, см. memory standalone_virtual_pnl_inflation §6.3).
# Если current_price УЖЕ на резолюционном экстремуме — рынок фактически разрешился,
# ликвидности для выхода нет. "Закрытие по take_profit" здесь = продажа по mid-цене
# резолюции (0.001/0.999), для которой НЕТ контрагента (NO-share не продать по ~$1.0
# когда YES выиграл). Это маскированная резолюция, записанная как TP — фантомная
# прибыль. Диагноз на проде: STANDALONE 6 сделок +$5520 (avg +920%/≤20мин),
# honest +$3311 → −$2208 без фантома. Механизм: high-edge bypass MZE → TP-ветка →
# fill по экстремуму.
#
# Fix (вариант A+hold): на экстремуме НЕ срабатывать take_profit ПРИ КОРОТКОМ hold —
# держать позицию до НАСТОЯЩЕЙ резолюции (settlement-путь, 1 нога, без фиктивной
# exit-продажи). SL и MZE НЕ трогаем: circuit-breaker SL нужен (D38), а SL-убыток на
# экстремуме реалистичен (досидел бы до резолюции → 0, тот же результат для
# проигравшей стороны). Порог цены жёстче mid-zone: это про "рынок разрешился".
# NB 2026-07-07: hold-to-resolution гейт (см. docstring run_position_manager) при
# virtual_early_exit_enabled=False (дефолт-политика) держит до резолюции ВСЕ
# exit_reason, включая этот circuit-breaker SL.
#
# Почему +hold (а не любой extreme): точный критерий фантома из прод-аудита (N2) =
# extreme + hold ≤20мин. Это локализует фантом (STANDALONE — весь ≤15мин) и НЕ бьёт
# D3 cross-market arb, где цена сходится к резолюции за ЧАСЫ (long-hold extreme =
# легитимное арбитражное схождение, его природа — см. memory: "для D3 multi-market =
# арбитраж by design, копировать guard вслепую нельзя"). Short-hold extreme у D3 (15
# мин → 0.001) тоже ловится — это тот же фантом, не арбитраж.
RESOLUTION_EXTREME_FLOOR: float = 0.02
RESOLUTION_EXTREME_CEILING: float = 0.98
# TP на экстремуме блокируется только если hold ≤ этого (фантом ≈ мгновенный «TP»;
# долгое удержание до экстремума = цена реально дошла, P&L близок к правде).
PHANTOM_TP_MAX_HOLD_MINUTES: float = 20.0

# High-edge bypass: если signal был с edge >= HIGH_EDGE_BYPASS_PCT, мы НЕ применяем
# mid_zone_exit. Логика: ML модель reckon'ует huge mispricing, и price drift в сторону
# fair_p — это и есть ожидаемое поведение, не "ушёл из зоны".
# Backtest evidence (2026-05-13, last 30d resolved):
#   STANDALONE_V2_LGB edge>=100pp: n=48, WR=87.5%, ROI +7227% — confirmed alpha.
HIGH_EDGE_BYPASS_PCT: float = 50.0

# Минимальная "выдержка" позиции перед exit eligibility.
# MIN_POSITION_AGE_MINUTES — общий гейт (через _OPEN_POSITIONS_SQL filter).
# SL имеет отдельный, более строгий гейт MIN_AGE_FOR_STOP_LOSS_MINUTES.
#
# Эмпирика 2026-05-18 (14d virtual data, см. инцидент в memory):
#   - SL <30мин: n=109, net = −$10,028 (avg −$92/trade).
#   - Counterfactual для резолвнутых SL <30мин: 27% позиций WIN'нули бы
#     если бы держали (+$11.4k counterfactual). SL вырезал valid позиции
#     на entry-noise drawdown.
#   - SL ≥60мин ведёт себя адекватно (рынок реально ушёл).
# Решение: запретить SL в первые 60 минут после fill. TP/MZE остаются с 10 мин
# (TP <30мин был +$2,757, нормальный upside, не трогаем).
MIN_POSITION_AGE_MINUTES: int = 10
MIN_AGE_FOR_STOP_LOSS_MINUTES: int = 60

# Per-detector overrides. Если detector НЕ в словаре — используем DEFAULT.
#
# Эмпирика 2026-05-19 (TP/SL audit на 30d virtual_settled, после VOIDED_BUG исключения):
#   Detector       n_tp  n_sl  TP_avg  SL_avg  actual_WR  breakeven_WR  status
#   D41_momentum     6     5    +78    -147     55%        65%         WR<breakeven → ослабляем SL
#   D3_cross_mkt     21   17    +180   -255     55%        59%         WR≈breakeven → ослабляем SL
#   D36_obi_press    40   63    +109    -75     39%        41%         Пограничный → уже WR>=39%, нужно
#                                                                       breakeven 35%, ослабляем SL
#   V2_LGB          22   24    +94    -100     48%        52%         Чуть-чуть ниже → mild tweak
#   D31_underdog     2   15    +386   -162     12%        29%         НЕ TP/SL проблема → paused (2026-05-19)
#   D37_depth_decay  6   18    +150   -127     25%        46%         НЕ TP/SL проблема → paused (2026-05-19)
#
# Цель overrides: снизить breakeven_WR ниже actual_WR на ≥5pp margin.
#
# Тройка (tp_pct, sl_pct, max_hold_hours | None) — max_hold_hours добавлен 2026-07-02
# (audit топ-1, live exit path): live-позиция без TP/SL триггера всё равно закрывается
# по достижении возраста. None = без max_hold (текущее поведение всех существующих
# записей — не менялось при добавлении 3-го элемента).
_DETECTOR_OVERRIDES: dict[str, tuple[float, float, float | None]] = {
    # D14 Crypto winners — validated mid-zone winner, тримминг 4h horizon, агрессивный.
    "D14_crypto_winners_distribute_no": (25.0, -15.0, None),
    #   D3: hold-to-resolution (human decision 2026-07-07). Старая политика (35, -15,
    #   эмпирика 2026-05-19) — худшая зона TP/SL-грида: exit-trajectories-audit-2026-07-06
    #   (n=114, hold +46.6%/сигнал, зона TP+30..50×SL−20 теряет 6-10 п.п. на всех haircut)
    #   + virtual-stage-audit-2026-07-05 (SL −$8.4k при 58 выходах, 21 срезанный winner).
    #   Пороги теперь catastrophic circuit-breaker по образцу D38, бизнес-выход —
    #   резолюция рынка (HOLD_TO_RESOLUTION_DETECTORS ниже).
    "D3_cross_mkt_arb": (200.0, -80.0, None),
    #   D36: WR=39% близко к breakeven 41%. Ослабляем SL → новый breakeven 35%.
    #        Margin 4pp (тонко, но направление верное).
    "D36_obi_pressure": (30.0, -15.0, None),
    #   STANDALONE_V2_LGB: actual 48% vs breakeven 52%. Mild SL relaxation.
    "STANDALONE_V2_LGB": (30.0, -18.0, None),
    # 2026-05-26 RCA (research/detector-rca-2026-05-26/):
    #   D38 signal-level WR=89.8% на 2320 сигналов, hardcoded cal_p=0.8994 — детектор
    #   работает. Но virtual stop_loss срабатывает на intraday drift ДО UMA resolution,
    #   уничтожая edge: 5/5 resolved stop_loss trades имели side == winner (signal
    #   correct, exit премат). D38 specializes в resolution_lag — позиции должны
    #   доходить до резолюции, а не выскакивать на noise.
    #   Решение: HOLD_TO_RESOLUTION (см. ниже) + circuit-breaker SL=-80% на случай
    #   разрыва модели. TP не нужен — edge раскрывается only at resolution.
    "D38_resolution_lag": (200.0, -80.0, None),
    # Paused/killed детекторы — не должны попасть сюда (signal fetcher отфильтрует),
    # fallback к default остаётся.
}

# Hold-to-resolution детекторы: skip mid_zone_exit (но TP/SL остаются — для них
# выставлены wide thresholds в _DETECTOR_OVERRIDES, чтобы срабатывали только на
# catastrophic divergence).
#
# Используется когда signal-level WR измерен at resolution, и intraday price
# drift — это NOISE, а не signal failure. D38_resolution_lag — образец: 89.8% WR
# на 2320 сигналов при condition "держим до UMA финализации".
#
# Эффект: позиции этих детекторов не выбрасываются по mid_zone_exit (price
# покинул [0.30, 0.85]). TP/SL остаются как circuit-breakers.
HOLD_TO_RESOLUTION_DETECTORS: frozenset[str] = frozenset(
    {
        "D38_resolution_lag",
        # D3 — 2026-07-07 (см. комментарий у _DETECTOR_OVERRIDES): арбитражное схождение
        # к экстремуму за часы — легитимный путь к резолюции, MZE его не выбрасывает.
        "D3_cross_mkt_arb",
    }
)


def _thresholds_for(detector: str) -> tuple[float, float]:
    """Return (tp_pct, sl_pct) for detector. Falls back to defaults.

    max_hold_hours (3-й элемент _DETECTOR_OVERRIDES) сюда не входит — см. _max_hold_for.
    Сигнатура (2-tuple) не менялась при расширении _DETECTOR_OVERRIDES до троек
    2026-07-02, чтобы не ломать существующих вызывающих/тесты.
    """
    tp, sl, _max_hold = _DETECTOR_OVERRIDES.get(detector, (DEFAULT_TP_PCT, DEFAULT_SL_PCT, None))
    return tp, sl


def _max_hold_for(detector: str) -> float | None:
    """Return max_hold_hours for detector, или None если не задан (без max_hold triggers)."""
    _tp, _sl, max_hold = _DETECTOR_OVERRIDES.get(detector, (DEFAULT_TP_PCT, DEFAULT_SL_PCT, None))
    return max_hold


_OPEN_POSITIONS_SQL = """
SELECT
    eo.id,
    eo.detector,
    eo.market_id,
    eo.side,
    eo.fill_price::float    AS fill_price,
    eo.stake::float         AS stake,
    eo.filled_at,
    eo.ev_pct::float        AS edge_pct,
    COALESCE(m.category, 'other') AS category,
    m.fee_schedule,
    m.current_price::float  AS current_price,
    m.resolved              AS market_resolved
  FROM execution_orders eo
  JOIN markets m ON m.polymarket_id = eo.market_id
 WHERE eo.status         = 'virtual_filled'
   AND eo.execution_mode = 'virtual'
   AND eo.exit_reason   IS NULL
   AND m.current_price  IS NOT NULL
   AND m.resolved        = false
   AND eo.filled_at      < now() - make_interval(mins => $1)
"""

# Live-контур (audit топ-1, 2026-07-02): та же форма, что _OPEN_POSITIONS_SQL, но
# status='filled' + execution_mode IN ('maker','taker') вместо virtual_filled/virtual.
# clob_token_ids добавлен для get_live_yes_price (Phase 2 Task 4) — WS-midpoint с
# fallback на m.current_price. За флагом settings.live_exit_enabled (config.py).
_OPEN_LIVE_POSITIONS_SQL = """
SELECT
    eo.id,
    eo.detector,
    eo.market_id,
    eo.side,
    eo.fill_price::float    AS fill_price,
    eo.stake::float         AS stake,
    eo.filled_at,
    eo.ev_pct::float        AS edge_pct,
    COALESCE(m.category, 'other') AS category,
    m.fee_schedule,
    m.current_price::float  AS current_price,
    m.resolved              AS market_resolved,
    m.clob_token_ids
  FROM execution_orders eo
  JOIN markets m ON m.polymarket_id = eo.market_id
 WHERE eo.status         = 'filled'
   AND eo.execution_mode IN ('maker', 'taker')
   AND eo.exit_reason   IS NULL
   AND m.current_price  IS NOT NULL
   AND m.resolved        = false
   AND eo.filled_at      < now() - make_interval(mins => $1)
"""


def _unrealized_pnl_pct(side: str, fill_price: float, current_price: float) -> float | None:
    """Compute current unrealized PnL percentage for an open virtual position.

    Contract:
      - fill_price хранится в координатах нашей стороны:
          YES side → fill_price = YES price at entry
          NO  side → fill_price = NO  price at entry = 1 − yes_price_at_fill
        См. detector_signal_service.py:42-47 + virtual_execution.py:135-136.
      - current_price из markets.current_price — это YES price (canonical).

    Formula:
      YES: (current_yes / fill - 1) × 100
      NO:  ((1 - current_yes) / fill - 1) × 100   ← exit value в NO координатах ÷ NO entry

    Returns None if math degenerate.
    """
    if side == "YES":
        if fill_price <= 0.0:
            return None
        return (current_price / fill_price - 1.0) * 100.0
    if side == "NO":
        if fill_price <= 0.0:
            return None
        no_current = 1.0 - current_price
        return (no_current / fill_price - 1.0) * 100.0
    return None


def _is_phantom_tp_exit(current_price: float, age_minutes: float) -> bool:
    """True если потенциальный take_profit-выход фантомный: цена на резолюционном
    экстремуме (рынок фактически разрешился, TP-fill по mid фиктивен — нет
    контрагента) И позиция держалась недолго (мгновенный «TP» ≠ реальное движение).

    Long-hold extreme НЕ фантом: цена дошла до экстремума за часы (для D3 — легит
    арбитражное схождение). См. константы выше + memory §6.3."""
    is_extreme = current_price <= RESOLUTION_EXTREME_FLOOR or current_price >= RESOLUTION_EXTREME_CEILING
    return is_extreme and age_minutes <= PHANTOM_TP_MAX_HOLD_MINUTES


def _compute_early_exit_pnl(
    *,
    side: str,
    stake: float,
    fill_price: float,
    exit_price: float,
    category: str,
    fee_schedule: dict | None = None,
) -> dict[str, float]:
    """Compute realized PnL when early-exiting a position.

    Contract (canonical):
      - fill_price = entry cost per share в координатах нашей стороны.
          YES side → fill_price = YES price at entry. shares = stake / YES_fill.
          NO  side → fill_price = NO  price at entry (= 1 − YES at fill). shares = stake / NO_fill.
        SSoT: detector_signal_service.py:42-47 + virtual_execution.py:135-136.
        Sanity: stake = shares × fill_price → invariant qty × fill ≈ stake в БД.
      - exit_price = current YES price из markets.current_price (canonical YES).
          Для NO position exit value per share = (1 − exit_price_YES).

    Early-exit (TP/SL/MZE) = активная продажа позиции в книге. ДВЕ trade ноги:
      entry: amount=stake, p=fill_price (зашли)
      exit:  amount=shares×exit_value, p=exit_value (продали)
    Поэтому fee = fee(entry) + fee(exit), slippage = 2 × stake × half_spread.

    Отличие от _compute_settlement_pnl (резолюция, 1 нога): резолюция — это
    pyout без trade, поэтому exit fee/slippage там не платится. См. fix 2026-05-18.
    """
    cat = (category or "other").lower()

    if side == "YES":
        eff_fill = max(fill_price, 0.001)
        shares = stake / eff_fill
        gross_pnl = shares * exit_price - stake
        entry_notional = stake
        exit_notional = shares * exit_price
        entry_price_for_fee = eff_fill
        exit_price_for_fee = max(exit_price, 0.001)
    elif side == "NO":
        eff_fill = max(fill_price, 0.001)
        shares = stake / eff_fill
        no_exit = max(1.0 - exit_price, 0.0)
        gross_pnl = shares * no_exit - stake
        entry_notional = stake
        exit_notional = shares * no_exit
        entry_price_for_fee = eff_fill
        exit_price_for_fee = max(no_exit, 0.001)
    else:
        return {"pnl": 0.0, "trading_fee_usd": 0.0, "slippage_usd": 0.0, "net_pnl": 0.0}

    fee_entry = estimate_trading_fee(entry_notional, entry_price_for_fee, cat, fee_schedule)
    fee_exit = estimate_trading_fee(exit_notional, exit_price_for_fee, cat, fee_schedule)
    fee_total = fee_entry + fee_exit

    # Slippage = stake-based, 2 legs (entry + exit для активной торговли на exit).
    # half_spread из get_spread_estimate() — ДОЛЯ цены (0.025 = 2.5%), не $/share.
    # 2026-05-18 был ошибочный rollover к shares × half_spread (intent: fix extreme
    # zones), но это даёт slippage > stake на penny markets (D43: $3,333 на $400).
    # Правильная семантика: slippage = stake × frac, легально интерпретируется
    # как "X% потери на каждой ноге торговли". Для depth-aware learning ходить
    # в app.core.slippage.apply_realistic_slippage (требует book_depth).
    half_spread = get_spread_estimate(cat)
    slippage = 2.0 * stake * half_spread

    net_pnl = gross_pnl - fee_total - slippage
    return {
        "pnl": round(gross_pnl, 4),
        "trading_fee_usd": round(fee_total, 4),
        "slippage_usd": round(slippage, 4),
        "net_pnl": round(net_pnl, 4),
    }


_EARLY_EXIT_UPDATE_SQL = """
UPDATE execution_orders
   SET status            = 'virtual_settled',
       outcome           = 'EARLY_EXIT',
       exit_reason       = $1,
       exit_price        = $2,
       exit_at           = now(),
       pnl               = $3,
       trading_fee_usd   = $4,
       slippage_usd      = $5,
       net_pnl           = $6,
       settled_at        = now()
 WHERE id     = $7
   AND status = 'virtual_filled'
"""


_STALE_PRICE_SKIP_REASONS: frozenset[str] = frozenset({"take_profit", "mid_zone_exit"})


def _decide_exit(
    *,
    detector: str,
    side: str,
    pnl_pct: float | None,
    current_price: float,
    age_minutes: float,
    edge_pct: float,
    max_hold_hours: float | None = None,
    is_live: bool = True,
) -> str | None:
    """Общая exit-логика для virtual и live-контура (audit топ-1, 2026-07-02):
    mid_zone_exit → take_profit (с фантом-guard) → stop_loss (с age-gate) → max_hold.

    Возвращает exit_reason ('mid_zone_exit' | 'take_profit' | 'stop_loss' | 'max_hold')
    или None (держим позицию). Чистая функция — без DB/side effects, чтобы одна и та же
    логика решения работала для virtual_filled (paper UPDATE) и filled/live
    (реальный SELL через close_live_position).

    max_hold_hours — новое (2026-07-02): если позиция старше него — закрыть вне
    зависимости от PnL. Проверяется ПОСЛЕ TP/SL/MZE (даёт им шанс сработать первыми
    на честной цене, max_hold — backstop от "вечно висящей" позиции).

    is_live — свежесть current_price (live-контур, 2026-07-02): get_live_yes_price
    падает на 15-минутный db_price, если WS-кэш протух. Асимметрия рисков:
      - take_profit/mid_zone_exit не защитные механизмы — на неопределённой
        цене НЕ действуем (skip, вернуть None), чтобы не зафиксировать
        решение на устаревших данных.
      - stop_loss НЕ блокируется — пропустить реальный обвал дороже, чем
        чуть ранний защитный выход на неточной цене.
      - max_hold не зависит от цены — не трогаем.
    Дефолт True сохраняет поведение virtual-пути (не передаёт этот параметр).
    """
    if pnl_pct is None:
        return None

    tp_pct, sl_pct = _thresholds_for(detector)
    is_high_edge = edge_pct >= HIGH_EDGE_BYPASS_PCT
    is_hold_to_resolution = detector in HOLD_TO_RESOLUTION_DETECTORS
    sl_eligible = age_minutes >= MIN_AGE_FOR_STOP_LOSS_MINUTES

    exit_reason: str | None = None
    in_extreme_zone = current_price <= MID_ZONE_FLOOR or current_price >= MID_ZONE_CEILING
    mze_bypass = is_high_edge or is_hold_to_resolution

    if in_extreme_zone and not mze_bypass:
        exit_reason = "mid_zone_exit"
    elif in_extreme_zone and mze_bypass:
        if pnl_pct >= tp_pct and not _is_phantom_tp_exit(current_price, age_minutes):
            exit_reason = "take_profit"
        elif pnl_pct <= sl_pct and sl_eligible:
            exit_reason = "stop_loss"
    elif pnl_pct >= tp_pct and not _is_phantom_tp_exit(current_price, age_minutes):
        exit_reason = "take_profit"
    elif pnl_pct <= sl_pct and sl_eligible:
        exit_reason = "stop_loss"

    if exit_reason is None and max_hold_hours is not None and (age_minutes / 60.0) > max_hold_hours:
        exit_reason = "max_hold"

    if not is_live and exit_reason in _STALE_PRICE_SKIP_REASONS:
        return None

    return exit_reason


async def run_position_manager() -> dict[str, Any]:
    """Iterate open virtual positions, trigger TP/SL or mid-zone exit gate.

    Age gates:
      - TP/MZE eligible после MIN_POSITION_AGE_MINUTES (10 мин).
      - SL eligible только после MIN_AGE_FOR_STOP_LOSS_MINUTES (60 мин) —
        в первые 60 мин SL skip'ается, позиция держится (n_sl_held_young).
        Эмпирика 2026-05-18: ранние SL вырезали 27% будущих winners.

    Exit hierarchy (first matching wins):
      1. **mid_zone_exit** — current_price ушёл из [MID_ZONE_FLOOR, MID_ZONE_CEILING],
         **И** edge_pct < HIGH_EDGE_BYPASS_PCT. High-edge signals не закрываются —
         ML reckon'ет huge mispricing, price движется К fair value, не "ушёл".
      2. **take_profit** — unrealized PnL >= tp_threshold for detector.
      3. **stop_loss** — unrealized PnL <= sl_threshold for detector.

    Hold-to-resolution гейт (закреплённая политика, human decision 2026-07-07):
    virtual early-exit (TP/SL/MZE) исполняется ТОЛЬКО при
    settings.virtual_early_exit_enabled=True (дефолт False) — virtual-позиции
    держатся до резолюции. Evidence: virtual-stage-audit-2026-07-05 (hold-
    контрфактика +127% vs as-is, D42 меняет знак) + exit-trajectories-audit-
    2026-07-06 (грид нигде уверенно не бьёт hold, SL порезал 145 будущих
    winners). История: 2026-07-05 (#648) гейт назывался «exit-паритетом» и
    висел на live_exit_enabled (virtual зеркалит live BUY-only); 2026-07-07
    отвязан на собственный флаг, чтобы включение live-exit НЕ реанимировало
    virtual TP/SL молча. Код TP/SL/MZE НЕ удалён — только гейт на ПРИНЯТИЕ
    решения. Гейт накрывает ВСЕ exit_reason, включая circuit-breaker SL −80%
    у D38/D3 (counterfactual: hold для D38 не хуже SL, −$10.4k vs −$12.1k).
    MtM-обновления и phantom-TP guard не затронуты — гейт применяется только
    к финальному exit_reason.
    """
    from app.core.config import get_settings

    early_exit_enabled = get_settings().virtual_early_exit_enabled

    rows = await fetch(_OPEN_POSITIONS_SQL, MIN_POSITION_AGE_MINUTES)
    if not rows:
        logger.info("position_manager_no_open_positions")
        summary: dict[str, Any] = {
            "checked": 0,
            "tp_closed": 0,
            "sl_closed": 0,
            "sl_held_young": 0,
            "mid_zone_exits": 0,
            "high_edge_kept": 0,
            "tp_held_extreme": 0,
            "skipped": 0,
        }
        await _maybe_run_live_contour(summary)
        return summary

    if not early_exit_enabled:
        logger.info(
            "position_manager_exit_parity_skip",
            reason="virtual_early_exit_enabled=False — virtual TP/SL/mid_zone_exit гейтятся, "
            "позиции держатся до резолюции (hold-to-resolution, human decision 2026-07-07)",
            checked=len(rows),
        )

    n_tp = n_sl = n_mid_exit = n_high_edge_kept = n_skipped = n_sl_held_young = 0
    n_tp_held_extreme = 0  # TP заблокирован на резолюционном экстремуме (фантом-guard)
    now = datetime.now(UTC)

    for r in rows:
        detector = r["detector"]
        side = r["side"]
        fill_price = float(r["fill_price"])
        current_price = float(r["current_price"])
        stake = float(r["stake"])
        edge_pct = float(r["edge_pct"]) if r["edge_pct"] is not None else 0.0

        pnl_pct = _unrealized_pnl_pct(side, fill_price, current_price)
        if pnl_pct is None:
            n_skipped += 1
            continue

        tp_pct, sl_pct = _thresholds_for(detector)
        is_high_edge = edge_pct >= HIGH_EDGE_BYPASS_PCT
        # hold_to_resolution: edge раскрывается только в момент UMA финализации
        # (D38 — RCA 2026-05-26 confirmed). Skip mid_zone_exit как high-edge.
        is_hold_to_resolution = detector in HOLD_TO_RESOLUTION_DETECTORS

        # SL имеет дополнительный age-gate: запрещён в первые
        # MIN_AGE_FOR_STOP_LOSS_MINUTES после fill. Это убирает entry-noise SL,
        # которые в 27% случаев резали бы winning позиции. TP/MZE — без изменений.
        age_minutes = (now - r["filled_at"]).total_seconds() / 60.0 if r["filled_at"] else 0.0
        sl_eligible = age_minutes >= MIN_AGE_FOR_STOP_LOSS_MINUTES

        exit_reason: str | None = None
        # Mid-zone exit gate с high-edge bypass + hold-to-resolution bypass.
        in_extreme_zone = current_price <= MID_ZONE_FLOOR or current_price >= MID_ZONE_CEILING
        mze_bypass = is_high_edge or is_hold_to_resolution
        if in_extreme_zone and not mze_bypass:
            exit_reason = "mid_zone_exit"
        elif in_extreme_zone and mze_bypass:
            # Высокий edge OR hold-to-resolution — НЕ закрываем по zone, ждём resolution или TP/SL.
            n_high_edge_kept += 1
            # Но всё равно проверяем TP/SL — может цена ушла слишком далеко.
            # TP-реализм guard: short-hold TP на резолюционном экстремуме фиктивен
            # (нет контрагента) — держим до настоящей резолюции. SL не трогаем.
            if pnl_pct >= tp_pct and not _is_phantom_tp_exit(current_price, age_minutes):
                exit_reason = "take_profit"
            elif pnl_pct >= tp_pct:
                # TP хотел сработать, но short-hold + экстремум → hold-to-resolution.
                n_tp_held_extreme += 1
                n_skipped += 1
                continue
            elif pnl_pct <= sl_pct and sl_eligible:
                exit_reason = "stop_loss"
            elif pnl_pct <= sl_pct:
                n_sl_held_young += 1
                n_skipped += 1
                continue
            else:
                n_skipped += 1
                continue
        elif pnl_pct >= tp_pct and not _is_phantom_tp_exit(current_price, age_minutes):
            exit_reason = "take_profit"
        elif pnl_pct >= tp_pct:
            # Short-hold TP на резолюционном экстремуме = фантом (см. _is_phantom_tp_exit).
            # Держим до настоящей резолюции вместо фиктивной продажи по mid.
            n_tp_held_extreme += 1
            n_skipped += 1
            continue
        elif pnl_pct <= sl_pct and sl_eligible:
            exit_reason = "stop_loss"
        elif pnl_pct <= sl_pct:
            n_sl_held_young += 1
            n_skipped += 1
            continue
        else:
            n_skipped += 1
            continue

        if not early_exit_enabled:
            # Hold-to-resolution: решение принято (exit_reason набран), но политика
            # early-exits не исполняет — держим позицию до резолюции.
            n_skipped += 1
            continue

        pnl = _compute_early_exit_pnl(
            side=side,
            stake=stake,
            fill_price=fill_price,
            exit_price=current_price,
            category=r["category"],
            fee_schedule=parse_jsonb(r.get("fee_schedule")),
        )

        try:
            await execute(
                _EARLY_EXIT_UPDATE_SQL,
                exit_reason,
                round(current_price, 4),
                pnl["pnl"],
                pnl["trading_fee_usd"],
                pnl["slippage_usd"],
                pnl["net_pnl"],
                r["id"],
            )
        except Exception as exc:
            logger.warning(
                "position_manager_update_failed",
                order_id=str(r["id"]),
                error=str(exc)[:200],
            )
            n_skipped += 1
            continue

        if exit_reason == "take_profit":
            n_tp += 1
        elif exit_reason == "stop_loss":
            n_sl += 1
        elif exit_reason == "mid_zone_exit":
            n_mid_exit += 1

        logger.info(
            "position_manager_early_exit",
            order_id=str(r["id"]),
            detector=detector,
            side=side,
            reason=exit_reason,
            fill_price=fill_price,
            exit_price=current_price,
            unrealized_pnl_pct=round(pnl_pct, 2),
            realized_net_pnl=pnl["net_pnl"],
        )

    summary = {
        "checked": len(rows),
        "tp_closed": n_tp,
        "sl_closed": n_sl,
        "sl_held_young": n_sl_held_young,  # 2026-05-18: SL triggered но позиция <60мин — оставили
        "mid_zone_exits": n_mid_exit,
        "high_edge_kept": n_high_edge_kept,
        "tp_held_extreme": n_tp_held_extreme,  # 2026-06-05: TP заблокирован на резолюц. экстремуме (фантом-guard)
        "skipped": n_skipped,
        "ts": now.isoformat(),
    }
    logger.info("position_manager_done", **summary)

    await _maybe_run_live_contour(summary)
    return summary


async def _maybe_run_live_contour(summary: dict[str, Any]) -> None:
    """Если settings.live_exit_enabled — прогнать live-контур и подмешать его summary
    в summary['live']. Гейт: ≥10 resolved live-ставок с net_pnl>=0 + human-решение
    (см. docs/superpowers/plans/2026-07-02-audit-fixes-phase3.md). Ошибка live-контура
    не должна ронять virtual-путь — только логируется.
    """
    from app.core.config import get_settings

    if not get_settings().live_exit_enabled:
        return
    try:
        summary["live"] = await run_live_position_manager()
    except Exception as exc:
        logger.error("position_manager_live_contour_failed", error=str(exc)[:200])
        await publish(
            [
                Notification(
                    severity=Severity.ACTION_REQUIRED,
                    subject="execution:position_manager",
                    kind="position_manager_crashed",
                    headline="Управление live-позициями упало",
                    what_happened=[
                        "Контур выходов не отработал: take-profit, stop-loss и max_hold не проверяются.",
                        f"Ошибка: {str(exc)[:200]}",
                    ],
                    what_if_ignored=(
                        "Открытые live-позиции не закроются по своим условиям, пока джоба не восстановится."
                    ),
                    what_to_do="Посмотри логи position_manager — контур перезапустится сам на следующем тике (5 мин).",
                    actions=[],
                    evidence=[f"error={str(exc)[:120]}"],
                )
            ],
            actor="position_manager",
        )


async def run_live_position_manager() -> dict[str, Any]:
    """Live-контур (audit топ-1, 2026-07-02): та же TP/SL/MZE-логика (_decide_exit),
    но исполнение выхода — реальный SELL через close_live_position (НЕ paper-UPDATE).

    За флагом settings.live_exit_enabled (проверяется вызывающей стороной —
    run_position_manager — и здесь же, если функция вызвана напрямую из cron/теста).
    Фактический realized PnL приходит позже, когда v2_execution_resolve увидит
    matched exit_order_id (status 'exiting' → 'settled').

    Частота тика (2026-07-02, live exit hardening): вызывается раз в 5 минут
    (тот же cron, что и virtual TP/SL, через _maybe_run_live_contour), НЕ раз
    в минуту как v2-execution-resolve. Осознанно: SELL всегда FOK по floor-цене,
    пересчитанной на момент отправки (fetch_live_book, WS+REST) — задержка тика
    влияет только на "когда заметим, что порог пробит", а не на цену исполнения
    самого SELL. Более частый выделенный cron добавил бы риск двойного
    конкурентного запуска этой функции (та же race-condition проблема, что
    close_live_position решает атомарным exit_pending барьером, но уже на
    уровне джобы). Ревизия — только по прод-данным после старта ramp.
    """
    from app.services.live_exit import close_live_position

    rows = await fetch(_OPEN_LIVE_POSITIONS_SQL, MIN_POSITION_AGE_MINUTES)
    summary = {"checked": len(rows), "exit_requested": 0, "exit_failed": 0, "skipped": 0, "skipped_stale_price": 0}
    if not rows:
        logger.info("position_manager_live_no_open_positions")
        return summary

    now = datetime.now(UTC)

    for r in rows:
        detector = r["detector"]
        side = r["side"]
        fill_price = float(r["fill_price"])
        stake = float(r["stake"])
        edge_pct = float(r["edge_pct"]) if r["edge_pct"] is not None else 0.0

        from app.detectors.token_resolver import yes_token_id
        from app.services.live_prices import get_live_yes_price

        db_price = float(r["current_price"])
        live_price, is_live = await get_live_yes_price(yes_token_id(r.get("clob_token_ids")), db_price)

        pnl_pct = _unrealized_pnl_pct(side, fill_price, live_price)
        age_minutes = (now - r["filled_at"]).total_seconds() / 60.0 if r["filled_at"] else 0.0
        max_hold_hours = _max_hold_for(detector)

        exit_reason = _decide_exit(
            detector=detector,
            side=side,
            pnl_pct=pnl_pct,
            current_price=live_price,
            age_minutes=age_minutes,
            edge_pct=edge_pct,
            max_hold_hours=max_hold_hours,
            is_live=is_live,
        )
        if exit_reason is None:
            if not is_live:
                # Не различаем здесь "решение было бы TP/MZE, но stale" от "цена
                # реально в mid-zone" — _decide_exit чистая функция без лога;
                # skipped_stale_price консервативно считает все skip на stale-тике,
                # достаточно для видимости масштаба (audit топ-1 доработка).
                summary["skipped_stale_price"] += 1
                logger.warning(
                    "position_manager_live_decision_stale_price_skip",
                    order_id=str(r["id"]),
                    detector=detector,
                    db_price=db_price,
                )
            else:
                summary["skipped"] += 1
            continue

        ok, msg = await close_live_position(r["id"], reason=exit_reason)
        if ok:
            summary["exit_requested"] += 1
            logger.info(
                "position_manager_live_exit_requested",
                order_id=str(r["id"]),
                detector=detector,
                side=side,
                reason=exit_reason,
                fill_price=fill_price,
                live_price=live_price,
                unrealized_pnl_pct=round(pnl_pct, 2) if pnl_pct is not None else None,
                stake=stake,
                exit_order_id=msg,
            )
        else:
            summary["exit_failed"] += 1
            logger.warning(
                "position_manager_live_exit_failed",
                order_id=str(r["id"]),
                detector=detector,
                reason=exit_reason,
                error=msg,
            )
            await publish(
                [
                    Notification(
                        severity=Severity.ACTION_REQUIRED,
                        # Subject свой у каждой позиции: с общим subject вторая
                        # неудача подавилась бы cooldown'ом первой, и про вторые
                        # застрявшие деньги владелец бы не узнал.
                        subject=f"order:{r['id']}",
                        kind="live_exit_failed",
                        headline="Не удалось закрыть live-позицию",
                        what_happened=[
                            f"Детектор {detector}, причина выхода: {exit_reason}.",
                            f"Ошибка: {str(msg)[:200]}",
                            "Позиция осталась открытой, попытка повторится на следующем тике (5 минут).",
                        ],
                        what_if_ignored=(
                            "Если ошибка повторяется, позиция не закроется по своим условиям — "
                            "ни по take-profit, ни по stop-loss."
                        ),
                        what_to_do=(
                            "Посмотри причину отказа: чаще всего это недоступный CLOB или "
                            "недостаточная ликвидность на нужной стороне."
                        ),
                        actions=[],
                        evidence=[f"order_id={r['id']}", f"reason={exit_reason}"],
                    )
                ],
                actor="position_manager",
            )

    logger.info("position_manager_live_done", **summary)
    return summary
