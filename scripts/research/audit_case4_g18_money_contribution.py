"""Phase 0 replay-кейс 4: контрфактический вклад G18 в деньгах (D14/D36).

Самый методологически тяжёлый из шести Phase 0 кейсов (docs/superpowers/plans/
2026-07-20-portfolio-agent-phase0.md, Task 7). Отвечает на вопрос: если бы G18
bin-калибровка (app/services/g18_bin_calibration.py) применялась к фактическим
резолвнутым сигналам D14/D36, как изменился бы гипотетический net_pnl — против
того, что фактически выдала бы fallback-цепочка LGB→Beta→raw без tier 0.

=== Step 0 (blocker-check, read-only SSH + git log, verified 2026-07-20) ===

Whitelist G18 (WHITELIST_DETECTORS в g18_bin_calibration.py) содержит ровно
D14_crypto_winners_distribute_no и D36_obi_pressure. Текущий detector_state:
    D14 stage=shadow (updated_at 2026-06-13, down-staged из virtual вручную)
    D36 stage=paused (paused_reason: "recent ΣROI<0 + Sortino<0 on live stream",
                       2026-06-24)
Ни один НЕ в live/virtual СЕЙЧАС. Но брифовый Step 0 спрашивает про "в каком-то
окне" — и здесь находка сложнее бинарного да/нет из брифа:

    execution_orders (детектор × execution_mode × status), verified на проде:
        D14: 1 order, virtual/virtual_settled, 2026-06-10, net_pnl=-154.65
        D36: 286 orders, virtual/virtual_settled, 2026-05-13..2026-05-28,
             сумма net_pnl=-9.88; ещё 5 virtual/rejected, 788 rejected/rejected
    execution_mode='live' — НОЛЬ строк для обоих детекторов (verified). Т.е.
    реальных денег (live) G18 НИКОГДА не касался — это подтверждает премис-кейс
    брифа. НО virtual (dry-run с полным sizing/fee/slippage конвейером) — было.

    Разбор virtual-окна показывает, что оно НЕ пригодно для честного paired-diff
    "как есть":
    1. G18 добавлен в whitelist для D14 с самого внедрения G18 (commit 2e0758e5,
       2026-05-25). Для D36 — только 2026-05-26 (commit 72453286, RCA). Но D36
       virtual-ордера идут с 2026-05-13 — т.е. ПОДАВЛЯЮЩЕЕ большинство (259/286,
       до 26 мая) физически исполнялись ДО того, как D36 вообще попал в G18
       whitelist. Подтверждено напрямую: ранние D36-ордера (2026-05-13) имеют
       calibrated_p == confidence == 0.6000 побитово — это raw/pass-through, не
       G18-бин (G18-бин дал бы wr_observed конкретного bin, не константу 0.6).
    2. execution_orders.outcome для virtual-периода содержит EARLY_EXIT (273/292)
       и VOIDED_BUG (4/292) вперемешку с чистыми resolution WIN/LOSS (15/292) —
       exit-логика (early_exit/bug) искажает net_pnl независимо от калибровки,
       что делает paired-diff по execution_orders.net_pnl нечестным (сравнение
       calibration-эффекта смешалось бы с exit-timing-эффектом).
    3. execution_orders НЕ хранит cal_source per-order (meta='{}' пусто везде,
       подтверждён brief-факт "cal_source нигде не персистится").

    Разрешение: соответствующий detector_signals (не execution_orders) для тех
    же детекторов НЕ имеет проблемы (2) — resolved/outcome там чистые WIN/LOSS/
    EXPIRED независимо от того, что сделал execution-слой (verified: 287/292
    virtual-ордеров имеют чистый WIN/LOSS в detector_signals, даже если
    execution_orders.outcome='EARLY_EXIT'). Ровно эта таблица и эта фильтрация
    — то, что брифовый Step 1 уже предписывает как источник данных, независимо
    от исхода Step 0. Поэтому: ПУТЬ = КОНТРФАКТИЧЕСКАЯ СИМУЛЯЦИЯ (Step 1-6 брифа
    как написаны), не прямой paired-diff по execution_orders — не потому что
    брифовая премиса "никогда не были virtual" подтвердилась буквально (она
    НЕ подтвердилась для virtual, только для live), а потому что чистая
    resolved-population в detector_signals методологически превосходит
    контаминированный execution_orders.net_pnl для ИЗОЛИРОВАННОГО измерения
    calibration-эффекта. `counterfactual=true` в metrics ниже отражает именно
    это — G18 не был на money-path (live) НИКОГДА, только на dry-run virtual
    path эпизодически и в основном ДО whitelist-инклюзии.

=== Метод (Step 1-6 брифа) ===

Step 1: detector_signals resolved=true, outcome IN ('WIN','LOSS') для D14/D36,
        весь доступный период (совпадает с REFIT_WINDOW_DAYS=90 семантикой
        calibration_bins_refit job, см. app/jobs/calibration_bins_refit.py, но
        здесь без ограничения окна — полная история, т.к. цель not "текущий
        живой бин", а "что было бы, если бы G18 стоял всегда").

Step 2: Для каждого сигнала — две калибровки:
    (а) G18 bin walk-forward — ТОЧНАЯ копия production-формулы bin_lookup()
        (app/services/g18_bin_calibration.py): calibrated_p = wr_observed бина
        (detector, category, price_bin) БЕЗ Bayes-blend, если n_observations
        >= MIN_OBS_FOR_LOOKUP (30) СРЕДИ СИГНАЛОВ ДО signal.generated_at (anti-
        look-ahead, walk-forward — паттерн из g18_calibration_replay.py). Если
        <30 в моменте t — считается как "G18 skip" в этот момент (используем
        fallback-значение и для G18-ветки тоже — G18 физически не сработал бы,
        это не искусственный provide). N_BINS=10, price floor — идентично обоим
        prod-модулям (g18_bin_calibration.py и calibration_bins_refit.py).
    (б) fallback-без-G18 — что фактически выдала бы app.calibration.fallback_chain
        .calibrate() БЕЗ tier 0 (G18 выключен), т.е. LGB→Beta→raw. Переиспользуем
        сам calibrate() напрямую (не реимплементируем цепочку), передав
        settings с calibration_bins_enabled=False — единственный тумблер,
        отключающий G18 tier в этой функции.

        ОГРАНИЧЕНИЕ (задокументировано честно, не скрыто): ml_calibrators /
        ml_calibrators_lgb — таблицы С ОДНОЙ ТЕКУЩЕЙ строкой на детектор
        (PRIMARY KEY(detector), fit_at — не история). LGB для D14/D36
        is_active=false везде (verified) → LGB tier всегда skip для этой пары
        детекторов, только Beta→raw актуальны. Beta-калибратор ЕСТЬ для обоих
        (verified), но это ТЕКУЩИЙ фит, не point-in-time-correct snapshot на
        момент каждого исторического сигнала — в отличие от G18-ветки, которая
        честно walk-forward. Это асимметрия метода: G18-плечо анти-look-ahead
        строгое, fallback-плечо использует текущий Beta-калибратор равномерно
        по всей истории (единственные персистентные данные, которые есть).
        Не искусственно занижает/завышает ни одну из сторон систематически
        (Beta статичен для обеих half-с-G18/без-G18 веток одинаково), но
        снижает точность абсолютного номинала дельты — направление и порядок
        величины остаются валидными ориентирами Phase 0, точное число — нет.

Step 3: Обе p прогоняются через size_from_calibrated_p() (app/calibration/
        ev_sizer.py) с entry_price=current_price (на момент сигнала),
        category (для честного fee+spread вычета внутри сайзера), bankroll_usd
        = фиксированный номинальный банк (см. NOTIONAL_BANKROLL_USD) — Phase 0
        не восстанавливает исторический банкролл по дням, фиксированный номинал
        достаточен для paired-сравнения (одна и та же bankroll на обеих ветках
        одного сигнала → делитель Kelly сокращается в дельте, влияет только на
        абсолютный $, не на знак/относительный порядок).
        net_pnl = WIN: stake*(payout-1) - fee - spread_cost;
                  LOSS: -stake - fee - spread_cost (fee/spread ВНУТРИ sizer'а
                  участвуют только в threshold-проверке net_ev_pct — это %
                  gate, не вычет из фактического $ payout; здесь отдельно
                  вычитаем ту же fee/spread как $-cost по факту сделки на
                  обеих ветках outcome, см. _fee_spread_cost_usd()).
        Skip (sizer вернул None, EV ниже порога) на любой из веток → сигнал
        исключается из paired-набора ЦЕЛИКОМ (иначе популяции разъедутся).

        НАХОДКА (обнаружена юнит-тестом на синтетике, tests/
        test_audit_case4_g18_money_contribution.py): MAX_STAKE_PCT_DEFAULT
        (5% bankroll) cap в size_from_calibrated_p() насыщается при f* > 0.20
        (т.к. fractional_kelly=0.25 × f* > 0.05 ⟺ f* > 0.20) — довольно
        типичное f* при meaningfully-edged calibrated_p. Практическое
        следствие: часть сигналов, где G18 и fallback дают РАЗНЫЙ calibrated_p,
        всё равно дадут delta_pnl=0, если Kelly-стейк ОБЕИХ веток упирается в
        cap независимо от точного p (только знак/близость к порогу edge
        реально влияют на delta в этом случае, не абсолютная величина p).
        Это НЕ баг скрипта — реальное поведение прод-сайзера, воспроизведённое
        честно; но означает, что итоговая |delta_pnl| систематически
        консервативнее (ближе к 0), чем "наивная" оценка без учёта cap
        предположила бы. Не корректируется искусственно — сайзер переиспользован
        as-is, как и предписывает бриф.

Step 4: paired-дельта Σnet_pnl_G18 - Σnet_pnl_fallback на ОДНИХ И ТЕХ ЖЕ
        сигналах (skip-фильтр Step 3 гарантирует общий набор).

Step 5: CI — per-event bootstrap, event_id-кластеризация, переиспользуем
        app.calibration.validation.event_cluster_bootstrap_roi (сидирован по
        умолчанию извне, seed=42 передаётся явно здесь — см. memory
        validation-bootstrap-seed-flaky-ci-2026-07-12, незасеянный
        default_rng(None) = флейк-мина). 1500 ресэмплов (между 1000-2000
        брифа). Bootstrap считается на delta_pnl СТРОКИ (не на ROI%), функция
        агностична к семантике колонки.

Step 6: agent_runs (kind='audit', name='g18_money_contribution'), verdict
        промаркирован 'counterfactual_unmeasured_in_reality' (live-путь G18
        никогда не касался).

Запускать ТОЛЬКО на проде, read-only роль (Task 2, oraclepoly_agent_ro), через
-m (см. audit_case3 docstring — иначе `from app...` падает):
    docker exec oraclepoly-backend python -m scripts.research.audit_case4_g18_money_contribution

До применения Task 1 (agent_runs/agent_claims) и Task 2 (роль + AGENT_DATABASE_URL)
на проде скрипт не подключится — init_db(dsn=settings.agent_database_url) упадёт,
роли ещё не существует. Ожидаемо, не баг этого скрипта (тот же паттерн, что в
audit_case1/2/3).
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import math
import sys
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

# Allow execution from repo root: PYTHONPATH=.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))

from app.calibration.beta_calibrator import BetaCalibrator  # noqa: E402
from app.calibration.ev_sizer import SizingDecision, size_from_calibrated_p  # noqa: E402
from app.calibration.fallback_chain import calibrate, load_l1_calibrators, load_lgb_calibrators  # noqa: E402
from app.calibration.validation import event_cluster_bootstrap_roi  # noqa: E402
from app.core.config import get_settings  # noqa: E402
from app.core.slippage import get_spread_estimate  # noqa: E402
from app.services.database import close_db, execute, fetch, fetchrow, init_db  # noqa: E402
from app.services.fee_calculator import estimate_trading_fee  # noqa: E402

# ── Целевые детекторы: G18 whitelist ровно эти два (verified 2026-07-20,
# app/services/g18_bin_calibration.py:48-58). Не расширять без пересверки whitelist. ──
TARGET_DETECTORS: tuple[str, ...] = ("D14_crypto_winners_distribute_no", "D36_obi_pressure")

# G18 bin-параметры — идентичны app/services/g18_bin_calibration.py и
# app/jobs/calibration_bins_refit.py (production SSoT). Не рассинхронивать.
N_BINS = 10
MIN_OBS_FOR_LOOKUP = 30
CATEGORIES_EXCLUDE = frozenset({"Minute Markets"})

# Anti-look-ahead gap — идентичен scripts/research/g18_calibration_replay.py
# (0 дней здесь сознательно отличается: production bin_lookup() не имеет gap,
# он читает detector_calibration_bins как есть на момент запроса; но эта
# таблица весь fit делает НЕДЕЛЬНО через calibration_bins_refit cron, что даёт
# естественный ~7-дневный лаг данные→бин на проде. Реплицируем этот же лаг
# явно, а не 0, чтобы не завысить G18-ветку "мгновенным" знанием исхода).
WEEKLY_REFIT_LAG_DAYS = 7

# Номинальный банкролл для paired-сравнения (Step 3 брифа) — фиксированный,
# не исторический (Phase 0 не восстанавливает банкролл по дням). Влияет только
# на абсолютный $ обеих веток одинаково (общий множитель), не на знак дельты.
NOTIONAL_BANKROLL_USD = 10_000.0

BOOTSTRAP_RESAMPLES = 1500
BOOTSTRAP_SEED = 42  # см. memory validation-bootstrap-seed-flaky-ci-2026-07-12

_RUN_NAME = "g18_money_contribution"
_QUERY_VERSION = "audit_case4_g18_money_contribution@1.0.0"


# ── Step 1: resolved-сигналы D14/D36 ─────────────────────────────────────────

_SIGNALS_SQL = """
SELECT
    ds.id,
    ds.detector,
    ds.market_id,
    ds.event_id,
    ds.side,
    ds.confidence::float AS confidence,
    ds.current_price::float AS current_price,
    ds.generated_at,
    ds.outcome,
    COALESCE(m.category, 'unknown') AS category
FROM detector_signals ds
LEFT JOIN markets m ON m.polymarket_id = ds.market_id
WHERE ds.detector = ANY($1)
  AND ds.resolved = true
  AND ds.outcome IN ('WIN', 'LOSS')
  AND ds.current_price IS NOT NULL
  AND ds.current_price > 0 AND ds.current_price < 1
ORDER BY ds.generated_at ASC
"""

_DATASET_HASH_SQL = """
SELECT count(*) AS n, max(ds.generated_at) AS max_generated_at
FROM detector_signals ds
WHERE ds.detector = ANY($1)
  AND ds.resolved = true
  AND ds.outcome IN ('WIN', 'LOSS')
"""


@dataclass(frozen=True)
class SignalRow:
    id: str
    detector: str
    market_id: str
    event_id: str | None
    side: str
    confidence: float
    current_price: float
    generated_at: datetime
    outcome: str
    category: str

    @property
    def won(self) -> bool:
        return self.outcome == "WIN"


async def load_signals() -> list[SignalRow]:
    rows = await fetch(_SIGNALS_SQL, list(TARGET_DETECTORS))
    return [
        SignalRow(
            id=str(r["id"]),
            detector=r["detector"],
            market_id=r["market_id"],
            event_id=r["event_id"],
            side=r["side"],
            confidence=float(r["confidence"]),
            current_price=float(r["current_price"]),
            generated_at=r["generated_at"],
            outcome=r["outcome"],
            category=r["category"],
        )
        for r in rows
    ]


# ── Step 2(а): G18 bin walk-forward (anti-look-ahead) ────────────────────────
#
# Точная копия production-формулы bin_lookup() — БЕЗ Bayes-combine (в отличие
# от старого scripts/research/g18_calibration_replay.py, который был написан
# ДО того, как production G18 устаканился на чистом wr_observed без blend, см.
# app/services/g18_bin_calibration.py:96-125). Здесь переиспользуется только
# time-aware walk-forward СКАФФОЛДИНГ (cutoff-цикл), не формула комбинирования.


def _price_bin(price: float) -> float:
    return float(math.floor(price * N_BINS) / N_BINS)


def compute_g18_walkforward(signals: list[SignalRow]) -> dict[str, tuple[float | None, int]]:
    """Для каждого signal.id -> (g18_p | None, n_historical_at_cutoff).

    g18_p=None означает "G18 бы пропустил" (skip_category / no_data /
    n<MIN_OBS_FOR_LOOKUP на момент cutoff) — ветка G18 в этом случае
    ЭКВИВАЛЕНТНА fallback-ветке (G18 физически не сработал бы), это не
    искусственный пропуск сигнала.
    """
    ordered = sorted(signals, key=lambda s: s.generated_at)
    out: dict[str, tuple[float | None, int]] = {}

    # Инкрементальный аккумулятор (detector, category, bin) -> (n, wins),
    # заполняется по мере прохода по времени — O(n), не O(n^2) полного
    # пересчёта на каждой итерации (что делал старый g18_calibration_replay.py
    # через df[mask] на каждой строке; здесь тот же anti-look-ahead результат
    # быстрее на текущем масштабе N~4000).
    from collections import defaultdict

    counts: dict[tuple[str, str, float], list[int]] = defaultdict(lambda: [0, 0])  # [n, wins]
    from collections import deque

    pending_q: deque[SignalRow] = deque()  # сигналы, ждущие пока не пройдёт WEEKLY_REFIT_LAG_DAYS

    for sig in ordered:
        cutoff = sig.generated_at - _lag()
        # Продвинуть очередь: все сигналы с generated_at <= cutoff уже "видны"
        # калибратору walk-forward (влить их в counts).
        while pending_q and pending_q[0].generated_at <= cutoff:
            done = pending_q.popleft()
            key = (done.detector, done.category, _price_bin(done.current_price))
            counts[key][0] += 1
            if done.won:
                counts[key][1] += 1

        key = (sig.detector, sig.category, _price_bin(sig.current_price))
        n, wins = counts[key]
        if sig.category in CATEGORIES_EXCLUDE:
            out[sig.id] = (None, n)
        elif n >= MIN_OBS_FOR_LOOKUP:
            out[sig.id] = (float(wins) / float(n), n)
        else:
            out[sig.id] = (None, n)

        pending_q.append(sig)

    return out


def _lag():
    from datetime import timedelta

    return timedelta(days=WEEKLY_REFIT_LAG_DAYS)


# ── Step 2(б): fallback-без-G18 через реальный fallback_chain.calibrate() ────


class _NoG18Settings:
    """Прокси над реальными settings с единственным переопределением —
    calibration_bins_enabled=False. calibrate() читает только этот атрибут
    из settings для тумблера G18-tier (см. fallback_chain.py:102)."""

    def __init__(self, real_settings: Any) -> None:
        self._real = real_settings
        self.calibration_bins_enabled = False

    def __getattr__(self, name: str) -> Any:
        return getattr(self._real, name)


async def compute_fallback_no_g18(signals: list[SignalRow]) -> dict[str, tuple[float, str]]:
    """calibrated_p через LGB->Beta->raw (G18 tier принудительно выключен).

    ОГРАНИЧЕНИЕ (см. docstring модуля): Beta/LGB — единственный ТЕКУЩИЙ фит
    (ml_calibrators/ml_calibrators_lgb — PK(detector), без истории), не
    point-in-time walk-forward. Применяется равномерно по всей истории для
    обеих сравниваемых веток одинаково.
    """
    real_settings = get_settings()
    no_g18_settings = _NoG18Settings(real_settings)

    cals: dict[str, BetaCalibrator] = await load_l1_calibrators()
    lgb_cals = await load_lgb_calibrators()

    out: dict[str, tuple[float, str]] = {}
    for sig in signals:
        row = {"current_price": sig.current_price, "category": sig.category, "confidence": sig.confidence}
        p, source = await calibrate(
            raw_p=sig.confidence,
            detector=sig.detector,
            cals=cals,
            row=row,
            lgb_cals=lgb_cals,
            settings=no_g18_settings,
        )
        out[sig.id] = (p, source)
    return out


# ── Step 3: sizing + fee/spread-adjusted net_pnl ─────────────────────────────


def _fee_spread_cost_usd(stake_usd: float, entry_price: float, category: str) -> float:
    """$-стоимость fee+spread на сделку (та же формула, что _cost_drag_pct в
    ev_sizer.py, но в $ вместо % — здесь используем напрямую fee_calculator +
    slippage, не переизобретаем: estimate_trading_fee на реальном notional,
    get_spread_estimate как half-spread доля от stake)."""
    payout = 1.0 / max(entry_price, 0.01)
    gross_notional = stake_usd * (1.0 + (payout - 1.0))
    fee_usd = 2.0 * estimate_trading_fee(gross_notional, entry_price, category)
    spread_usd = get_spread_estimate(category) * stake_usd
    return fee_usd + spread_usd


def _net_pnl_for_signal(decision: SizingDecision, won: bool, category: str) -> float | None:
    """None если sizer skip'нул (EV ниже порога) — сигнал исключается из paired-набора."""
    if decision.stake_usd is None:
        return None
    stake = decision.stake_usd
    payout = 1.0 / decision.entry_price
    cost = _fee_spread_cost_usd(stake, decision.entry_price, category)
    if won:
        gross = stake * (payout - 1.0)
        return round(gross - cost, 4)
    return round(-stake - cost, 4)


@dataclass(frozen=True)
class PairedResult:
    signal_id: str
    event_id: str | None
    detector: str
    net_pnl_g18: float
    net_pnl_fallback: float
    delta: float
    g18_p: float
    fallback_p: float
    fallback_source: str
    n_historical_at_cutoff: int


def build_paired_results(
    signals: list[SignalRow],
    g18_walkforward: dict[str, tuple[float | None, int]],
    fallback_calib: dict[str, tuple[float, str]],
) -> tuple[list[PairedResult], dict[str, int]]:
    """Step 3+4: sizing на обеих ветках, skip-фильтр по общему набору, paired delta.

    skip_reasons считает почему сигнал не попал в общий набор (для честной
    отчётности, не молчаливое усечение).
    """
    results: list[PairedResult] = []
    skip_reasons: dict[str, int] = {"g18_skip": 0, "fallback_skip": 0, "ok": 0}

    for sig in signals:
        g18_p_raw, n_hist = g18_walkforward.get(sig.id, (None, 0))
        fallback_p, fallback_source = fallback_calib.get(sig.id, (sig.confidence, "raw"))

        # Если G18 бы пропустил (нет данных на cutoff) — G18-ветка ЭКВИВАЛЕНТНА
        # fallback-ветке (тот же p, тот же source): нет разницы, значит delta=0
        # по построению для этого сигнала, а не искусственный skip.
        g18_p = g18_p_raw if g18_p_raw is not None else fallback_p

        g18_decision = size_from_calibrated_p(
            calibrated_p=g18_p,
            entry_price=sig.current_price,
            bankroll_usd=NOTIONAL_BANKROLL_USD,
            category=sig.category,
        )
        fallback_decision = size_from_calibrated_p(
            calibrated_p=fallback_p,
            entry_price=sig.current_price,
            bankroll_usd=NOTIONAL_BANKROLL_USD,
            category=sig.category,
        )

        net_g18 = _net_pnl_for_signal(g18_decision, sig.won, sig.category)
        net_fb = _net_pnl_for_signal(fallback_decision, sig.won, sig.category)

        if net_g18 is None and net_fb is None:
            skip_reasons["g18_skip"] += 1
            skip_reasons["fallback_skip"] += 1
            continue
        if net_g18 is None:
            skip_reasons["g18_skip"] += 1
            continue
        if net_fb is None:
            skip_reasons["fallback_skip"] += 1
            continue

        skip_reasons["ok"] += 1
        results.append(
            PairedResult(
                signal_id=sig.id,
                event_id=sig.event_id,
                detector=sig.detector,
                net_pnl_g18=net_g18,
                net_pnl_fallback=net_fb,
                delta=round(net_g18 - net_fb, 4),
                g18_p=g18_p,
                fallback_p=fallback_p,
                fallback_source=fallback_source,
                n_historical_at_cutoff=n_hist,
            )
        )

    return results, skip_reasons


# ── Step 5: per-event bootstrap CI на дельте ─────────────────────────────────


def bootstrap_delta_ci(paired: list[PairedResult]) -> tuple[float, float]:
    """event_cluster_bootstrap_roi агностична к семантике колонки — передаём
    delta_pnl per row вместо ROI%, кластеризация та же (event_id).

    Сигналы без event_id (NULL) кластеризуются как отдельные one-row "события"
    (используем signal_id как fallback-ключ группировки) — консервативно
    (не даёт им искусственно скоррелироваться друг с другом под одним
    fallback-ключом "unknown").
    """
    if not paired:
        return (0.0, 0.0)
    event_keys = [p.event_id if p.event_id else f"__signal__{p.signal_id}" for p in paired]
    deltas = [p.delta for p in paired]
    return event_cluster_bootstrap_roi(
        event_keys, deltas, n_resamples=BOOTSTRAP_RESAMPLES, confidence=0.95, seed=BOOTSTRAP_SEED
    )


# ── Step 6: agent_runs / agent_claims ────────────────────────────────────────


def _config_hash() -> str:
    payload = json.dumps(
        {
            "detectors": list(TARGET_DETECTORS),
            "n_bins": N_BINS,
            "min_obs_for_lookup": MIN_OBS_FOR_LOOKUP,
            "weekly_refit_lag_days": WEEKLY_REFIT_LAG_DAYS,
            "notional_bankroll_usd": NOTIONAL_BANKROLL_USD,
            "bootstrap_resamples": BOOTSTRAP_RESAMPLES,
            "bootstrap_seed": BOOTSTRAP_SEED,
            "sql": _SIGNALS_SQL,
        },
        sort_keys=True,
    )
    return hashlib.sha256(payload.encode()).hexdigest()[:16]


async def _dataset_hash() -> str:
    row = await fetchrow(_DATASET_HASH_SQL, list(TARGET_DETECTORS))
    n = row["n"] if row else 0
    max_gen = row["max_generated_at"] if row else None
    return hashlib.sha256(f"{n}:{max_gen}".encode()).hexdigest()[:16]


def _build_verdict(
    per_detector: dict[str, dict[str, Any]],
    delta_pnl: float,
    ci_low: float,
    ci_high: float,
    n_total: int,
    skip_reasons: dict[str, int],
) -> str:
    stage_info = "D14=shadow (down-staged 2026-06-13), D36=paused (2026-06-24, ΣROI<0+Sortino<0)"
    lines = [
        "verdict_tag: counterfactual_unmeasured_in_reality",
        f"G18 contribution to realized (live) PnL = $0 — {stage_info}, execution_mode='live' "
        "содержит НОЛЬ строк для обоих детекторов за всю историю (verified 2026-07-20). "
        "G18 никогда не влиял на реальные деньги для D14/D36.",
        f"Counterfactual paired-delta (G18 walk-forward vs fallback LGB→Beta→raw, "
        f"n={n_total} общих сигналов) = ${delta_pnl:+.2f} [95% CI {ci_low:+.2f}, {ci_high:+.2f}] "
        "(per-event bootstrap, 1500 ресэмплов, seed=42).",
        "Step 0 nuance (не буквальное подтверждение брифовой премисы 'никогда не были "
        "virtual'): оба детектора ИМЕЛИ execution_mode='virtual' исторически (D14: 1 order "
        "2026-06-10; D36: 286 orders 2026-05-13..05-28), но эти данные НЕ использовались для "
        "paired-diff напрямую — execution_orders.net_pnl там контаминирован EARLY_EXIT/"
        "VOIDED_BUG exit-артефактами (273+4 из 292 D36-ордеров), а большинство D36 virtual-"
        "трейдов (259/286) исполнялись ДО того, как D36 попал в G18 whitelist (2026-05-26) — "
        "т.е. физически не могли использовать G18 даже гипотетически в тот момент. "
        "detector_signals (resolved WIN/LOSS, не execution-слой) — чистый источник, "
        "использован вместо этого.",
        f"skip breakdown: g18_skip={skip_reasons.get('g18_skip', 0)} "
        f"fallback_skip={skip_reasons.get('fallback_skip', 0)} ok={skip_reasons.get('ok', 0)} "
        "(сигнал исключён из paired-набора если ЛЮБАЯ из веток дала EV ниже порога сайзера — "
        "иначе популяции разъехались бы).",
        "Известное ограничение метода: fallback-ветка (Beta-калибратор) использует ТЕКУЩИЙ "
        "fit (ml_calibrators — PK(detector), без истории по датам), не point-in-time-correct "
        "walk-forward, в отличие от G18-ветки (честный anti-look-ahead с 7-дневным лагом, "
        "имитирующим weekly calibration_bins_refit cron). Абсолютная величина дельты — "
        "ориентировочная; знак и порядок величины — единственное, на что можно полагаться.",
    ]
    for det, m in per_detector.items():
        lines.append(
            f"{det}: n={m['n']} delta_pnl=${m['delta_pnl']:+.2f} "
            f"net_pnl_g18=${m['sum_net_pnl_g18']:+.2f} net_pnl_fallback=${m['sum_net_pnl_fallback']:+.2f}"
        )
    return "\n".join(lines)


_INSERT_RUN_SQL = """
INSERT INTO agent_runs (kind, name, config_hash, dataset_hash, query_version, metrics, verdict, finished_at)
VALUES ('audit', $1, $2, $3, $4, $5::jsonb, $6, now())
RETURNING id
"""

_CLAIM_STATEMENT = (
    "G18 contribution to realized PnL = $0 (never in money-path for D14/D36); "
    "counterfactual delta vs fallback calibration measured with CI"
)

_UPSERT_CLAIM_SQL = """
INSERT INTO agent_claims (statement, scope, status, evidence_run_ids, verify_policy, verify_halflife_days)
VALUES ($1, 'policy:g18_calibration', 'active', ARRAY[$2]::bigint[], 'time_halflife', 30)
ON CONFLICT (statement) DO UPDATE
   SET evidence_run_ids = agent_claims.evidence_run_ids || EXCLUDED.evidence_run_ids,
       last_verified_at = now(),
       updated_at = now()
"""


async def run_audit() -> dict[str, Any]:
    signals = await load_signals()
    if not signals:
        return {
            "metrics": {"n_d14": 0, "n_d36": 0, "delta_pnl": None, "ci_low": None, "ci_high": None},
            "verdict": "NO DATA: 0 resolved WIN/LOSS signals for D14/D36.",
            "paired": [],
        }

    g18_walkforward = compute_g18_walkforward(signals)
    fallback_calib = await compute_fallback_no_g18(signals)
    paired, skip_reasons = build_paired_results(signals, g18_walkforward, fallback_calib)

    ci_low, ci_high = bootstrap_delta_ci(paired)
    delta_pnl = round(sum(p.delta for p in paired), 4)

    per_detector: dict[str, dict[str, Any]] = {}
    for det in TARGET_DETECTORS:
        det_rows = [p for p in paired if p.detector == det]
        per_detector[det] = {
            "n": len(det_rows),
            "delta_pnl": round(sum(p.delta for p in det_rows), 4),
            "sum_net_pnl_g18": round(sum(p.net_pnl_g18 for p in det_rows), 4),
            "sum_net_pnl_fallback": round(sum(p.net_pnl_fallback for p in det_rows), 4),
        }

    n_d14 = per_detector.get("D14_crypto_winners_distribute_no", {}).get("n", 0)
    n_d36 = per_detector.get("D36_obi_pressure", {}).get("n", 0)

    metrics: dict[str, Any] = {
        "delta_pnl": delta_pnl,
        "ci_low": round(ci_low, 4),
        "ci_high": round(ci_high, 4),
        "n_d14": n_d14,
        "n_d36": n_d36,
        "n_total_paired": len(paired),
        "counterfactual": True,
        "whitelisted_stages": {"d14": "shadow", "d36": "paused"},
        "skip_reasons": skip_reasons,
        "per_detector": per_detector,
        "notional_bankroll_usd": NOTIONAL_BANKROLL_USD,
        "bootstrap_resamples": BOOTSTRAP_RESAMPLES,
        "bootstrap_seed": BOOTSTRAP_SEED,
        "weekly_refit_lag_days": WEEKLY_REFIT_LAG_DAYS,
        # Historical virtual-order footnote (Step 0 nuance) — не используется для
        # расчёта, только для аудируемости verdict-текста.
        "historical_virtual_orders_footnote": {
            "d14_virtual_orders": 1,
            "d36_virtual_orders": 286,
            "d36_virtual_orders_pre_whitelist": 259,
            "live_orders_ever": 0,
        },
    }

    verdict = _build_verdict(per_detector, delta_pnl, ci_low, ci_high, len(paired), skip_reasons)

    return {"metrics": metrics, "verdict": verdict, "paired": paired}


async def persist(result: dict[str, Any]) -> int:
    """Записать agent_runs + upsert agent_claims. Read-only роль oraclepoly_agent_ro
    имеет точечный GRANT INSERT/UPDATE на agent_* таблицы (Task 2)."""
    config_hash = _config_hash()
    dataset_hash = await _dataset_hash()

    run_row = await fetch(
        _INSERT_RUN_SQL,
        _RUN_NAME,
        config_hash,
        dataset_hash,
        _QUERY_VERSION,
        json.dumps(result["metrics"], default=str),
        result["verdict"],
    )
    run_id = run_row[0]["id"]

    await execute(_UPSERT_CLAIM_SQL, _CLAIM_STATEMENT, run_id)

    return run_id


async def _run(*, dry_run: bool) -> dict[str, Any]:
    settings = get_settings()
    dsn = settings.agent_database_url
    if not dsn:
        print(
            "WARNING: agent_database_url не задан — read-only роль oraclepoly_agent_ro "
            "ещё не сконфигурирована (Task 2). Скрипт НЕ должен молча уходить под "
            "основным DATABASE_URL для read-only аудита.",
            file=sys.stderr,
        )
        sys.exit(1)

    await init_db(dsn=dsn)
    try:
        result = await run_audit()
        run_id = None
        if not dry_run and result["paired"]:
            run_id = await persist(result)
        return {**result, "run_id": run_id}
    finally:
        await close_db()


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Посчитать и напечатать метрики, но не писать в agent_runs/agent_claims.",
    )
    args = parser.parse_args()

    result = asyncio.run(_run(dry_run=args.dry_run))
    # paired содержит dataclass-инстансы — не JSON-serializable напрямую, убираем
    # из финального print (уже свёрнуто в metrics.per_detector).
    printable = {k: v for k, v in result.items() if k != "paired"}
    print(json.dumps(printable, indent=2, default=str))


if __name__ == "__main__":
    main()
