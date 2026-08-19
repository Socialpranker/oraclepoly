"""Detector signal source для strategy_position_manager.

Читает свежие unresolved detector_signals (за last hour), применяет
L1 Beta calibrator (если fit'ed) и L2 LightGBM meta-labeler (если active),
возвращает signals в формате compatible с execution pipeline.

Cold-start fallback (когда L1/L2 ещё не fit'ed):
- L1 missing → calibrated_p = raw_confidence
- L2 missing → meta_p = calibrated_p
- EV-sizer использует более консервативный fractional Kelly (f=0.10)
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from typing import Any

import asyncpg
import structlog

from app.calibration.fallback_chain import CAL_SOURCES
from app.calibration.fallback_chain import calibrate as _calibrate
from app.calibration.fallback_chain import load_l1_calibrators as _load_l1_calibrators
from app.calibration.fallback_chain import load_lgb_calibrators as _load_lgb_calibrators
from app.calibration.meta_labeler import (
    MetaLabelerExample,
    deserialize_from_json,
)
from app.core.config import get_settings
from app.jobs.ml_calibrator_fit import _PER_ANCHOR_DETECTORS
from app.services.database import fetch, fetchrow
from app.services.jsonb_utils import parse_jsonb

logger = structlog.get_logger()

V2_CUTOVER = datetime(2026, 4, 28, tzinfo=UTC)

# Лимит ожидания блокировки markets в cron-выборке сигналов. VACUUM FULL markets
# (вс 04:00 UTC) держит ACCESS EXCLUSIVE lock ~2 мин; при коллизии тик отдаёт []
# и падает не job, а один тик — следующий отработает после разлока.
_FETCH_LOCK_TIMEOUT_MS = 3000


def _opt_float(val: Any) -> float | None:
    """None-safe float конверсия для optional Tier 3.1 фич (БД → None/Decimal)."""
    return None if val is None else float(val)


# Общая основа для двух вариантов выборки: список свежих сигналов (execution
# cron/тик) и один сигнал по id (execution-демон, Phase 3 Task 3 — audit топ-7).
# Оба варианта ДОЛЖНЫ фильтровать одинаково (age/cooldown/stage) — иначе
# демон обработает сигнал, который cron бы отбросил (или наоборот). ORDER BY
# не входит в основу: для выборки по id он не нужен (ровно одна строка).
_SIGNAL_SELECT_BASE_SQL = """
SELECT ds.id,
       ds.detector,
       ds.market_id,
       ds.event_id,
       ds.side,
       ds.confidence       AS raw_p,
       ds.expected_edge_pct,
       ds.fair_price,
       ds.current_price,    -- LGB feature builder — отдельно от entry_price
       ds.event_size,
       -- entry_price = стоимость 1 share нашей стороны.
       -- ds.current_price хранится как YES-цена; для NO платим (1 - YES).
       -- Это критично для EV-sizer: payout = 1/entry_price.
       CASE WHEN ds.side = 'YES' THEN ds.current_price
            ELSE 1.0 - ds.current_price
       END                  AS entry_price,
       ds.horizon_sec,
       -- Резолвим token_id: если в detector_signals есть — берём оттуда (D3),
       -- иначе из markets.clob_token_ids[0=YES, 1=NO] (D4/D5/D6/D6b/D1).
       COALESCE(
           ds.token_id,
           CASE
               WHEN ds.side = 'YES' THEN m.clob_token_ids->>0
               WHEN ds.side = 'NO'  THEN m.clob_token_ids->>1
           END
       ) AS token_id,
       -- YES-токен для live-price телеметрии: get_live_yes_price ждёт YES-токен,
       -- а token_id выше — токен ТОРГУЕМОЙ стороны (для NO это NO-токен).
       m.clob_token_ids->>0 AS yes_token_id,
       ds.evidence,
       ds.generated_at,
       m.category,
       -- temperature_blacklist гейт (v2_execution): матчинг по тексту вопроса,
       -- т.к. temperature-рынки прячутся внутри категории World & Economy.
       m.question,
       m.fee_schedule,
       m.volume_24h,
       m.end_date          AS market_end_date,
       -- Tier 3.1: hours_to_resolution на момент signal (для LGB feature)
       CASE
           WHEN m.end_date IS NOT NULL
           THEN EXTRACT(epoch FROM (m.end_date - ds.generated_at)) / 3600.0
           ELSE NULL
       END AS hours_to_resolution_at_signal,
       -- Tier 3.1: rolling 30d window метрики (strictly < generated_at — leak-free
       -- даже для inference signal, у которого generated_at ≤ now)
       rw.recent_wr_30d,
       rw.detector_recent_count_30d,
       -- Tier 3.1: медиана settle-времени (detector,category) по virtual_settled
       -- ордерам strictly до signal. NULL если ордеров нет (shadow-детекторы).
       sv.settle_velocity_hours
  FROM detector_signals ds
  LEFT JOIN markets m ON m.polymarket_id = ds.market_id
  -- Tier 3.1 rolling window: WR и count детектора за 30d перед signal
  LEFT JOIN LATERAL (
      SELECT
          AVG(CASE WHEN ds2.outcome = 'WIN' THEN 1.0 ELSE 0.0 END) AS recent_wr_30d,
          COUNT(*) AS detector_recent_count_30d
        FROM detector_signals ds2
       WHERE ds2.detector = ds.detector
         AND ds2.resolved = true
         AND ds2.outcome IN ('WIN', 'LOSS')
         AND ds2.generated_at < ds.generated_at
         AND ds2.generated_at >= ds.generated_at - interval '30 days'
  ) rw ON true
  LEFT JOIN LATERAL (
      SELECT PERCENTILE_CONT(0.5) WITHIN GROUP (
                 ORDER BY EXTRACT(epoch FROM (eo.settled_at - eo.filled_at)) / 3600.0
             ) AS settle_velocity_hours
        FROM execution_orders eo
        LEFT JOIN markets m2 ON m2.polymarket_id = eo.market_id
       WHERE eo.detector = ds.detector
         AND COALESCE(m2.category, 'unknown') = COALESCE(m.category, 'unknown')
         AND eo.status = 'virtual_settled'
         AND eo.filled_at IS NOT NULL
         AND eo.settled_at IS NOT NULL
         AND eo.settled_at < ds.generated_at
         AND COALESCE((eo.meta->>'reentry_excluded')::bool, false) = false
  ) sv ON true
  -- Lifecycle filter: shadow stage не идёт через v2_execution
  -- (только эмитит signals; virtual/live → проходит весь pipeline).
  -- См. docs/detector_lifecycle.md.
  JOIN detector_state dst ON dst.detector = ds.detector
                          AND dst.stage IN ('virtual', 'live')
 WHERE ds.resolved = false
   AND ds.generated_at > now() - make_interval(secs => ds.horizon_sec)
   -- Hard staleness cap (2026-05-13): signal старше 6h не исполняем — adverse selection.
   -- Polymarket prices сильно двигаются за день; signal с current_price=0.5
   -- зафиксирован в БД, но book уже на 0.99 → drift gate отбивает 96% signals.
   -- 6h — компромисс между coverage и risk decay.
   AND ds.generated_at > now() - interval '6 hours'
   -- Task 2 (audit топ-4): dedup vs retry-cooldown. Терминальный reject
   -- (is_transient_reject=false) закрывает сигнал навсегда — прежнее поведение.
   -- Транзиентный reject (kill_switch/place_error/... — см. reject_classify.py)
   -- даёт новую попытку раз в ≥3 минуты, пока страховочный cap (1800s в
   -- check_signal_age, anti-selection fix 2026-07-22) не закроет окно —
   -- до ~10 попыток на сигнал (было ~2 при старом 300s-гейте).
   AND NOT EXISTS (
       SELECT 1 FROM execution_orders eo
        WHERE eo.detector_signal_id = ds.id
          AND (
               eo.status NOT IN ('rejected', 'cancelled')      -- активный ордер уже есть
            OR eo.is_transient_reject = false                  -- терминальный reject: сигнал закрыт
            OR eo.created_at > now() - interval '3 minutes'    -- транзиентный: cooldown между попытками
          )
   )
"""

# Список свежих сигналов (execution cron/тик): live-сигналы приоритетом над
# virtual-симуляциями в общем execution-цикле (audit топ-7, инцидент D38:
# 12-21 ордеров/мин забивали тик). Внутри stage — edge DESC (anti-selection
# fix 2026-07-22): лучшие сигналы конкурируют за лимит-слоты (daily_loss/
# max_open) первыми, вместо FIFO по generated_at (воронка отсеивала winners
# и пропускала losers — см. память d3-funnel-anti-selection). edge = raw
# confidence минус стоимость входа нашей стороны (calibrated_p в SQL-слое
# нет — калибровка идёт в Python после fetch). generated_at ASC — tiebreaker
# при равном edge (и по-прежнему ближе к дедлайну страховочного age-cap
# 1800s, check_signal_age). NULL-safety: ds.confidence и ds.current_price сейчас
# NOT NULL (миграция 20260430141632) — GREATEST(NULL, 0) недостижим на
# текущей схеме. NULLS LAST добавлен явной защитой на будущее: Postgres
# default для DESC — NULLS FIRST, то есть без явного NULLS LAST будущий
# nullable-сигнал (NULL edge) обошёл бы всех и попал в лимит-слот первым —
# ровно anti-selection, который этот таск чинит.
_FETCH_FRESH_SIGNALS_SQL = (
    _SIGNAL_SELECT_BASE_SQL
    + """ ORDER BY (dst.stage = 'live') DESC,
       GREATEST(ds.confidence - CASE WHEN ds.side = 'YES' THEN ds.current_price
                                     ELSE 1.0 - ds.current_price END, 0) DESC NULLS LAST,
       ds.generated_at ASC
"""
)

# Один сигнал по id (execution-демон, Phase 3 Task 3 — audit топ-7): та же
# основа (age/cooldown/stage), без ORDER BY — ровно одна строка. AND ds.id=$1
# добавлен последним, чтобы не мешать индексам основы (detector_signals PK).
_FETCH_SIGNAL_BY_ID_SQL = (
    _SIGNAL_SELECT_BASE_SQL
    + """   AND ds.id = $1
"""
)


async def _emit_cal_anchor_shadow(
    *,
    detector: str,
    signal_id: str,
    market_id: Any,
    raw_p: float,
    row: dict[str, Any],
    cals: dict[str, Any],
    lgb_cals: dict[str, Any],
    settings: Any,
    flat_p: float,
    flat_source: str,
) -> None:
    """Shadow-калибровка per-anchor модели — НЕ торгуемый путь.

    Задача 1.1 (roadmap аудита): читает evidence.anchor_hours сигнала и, если
    detector входит в _PER_ANCHOR_DETECTORS, повторно вызывает calibrate() с
    anchor_hours для сравнения anchor_p vs боевого flat_p. Результат только
    логируется (cal_anchor_shadow) — meta_p/calibrated_p, идущие в EV-sizer,
    этот вызов не трогает. Ошибка здесь не должна ломать основной путь.
    """
    if detector not in _PER_ANCHOR_DETECTORS:
        return
    evidence = parse_jsonb(row.get("evidence"))
    if not evidence:
        return
    anchor_hours = evidence.get("anchor_hours")
    if not anchor_hours:
        return
    try:
        anchor_hours_int = int(anchor_hours)
    except (TypeError, ValueError):
        return
    try:
        anchor_p, anchor_source = await _calibrate(
            raw_p, detector, cals, row=row, lgb_cals=lgb_cals, anchor_hours=anchor_hours_int, settings=settings
        )
        logger.info(
            "cal_anchor_shadow",
            detector=detector,
            signal_id=signal_id,
            market_id=market_id,
            anchor_hours=anchor_hours_int,
            flat_p=flat_p,
            flat_source=flat_source,
            anchor_p=anchor_p,
            anchor_source=anchor_source,
        )
    except Exception as exc:
        logger.warning(
            "cal_anchor_shadow_failed",
            detector=detector,
            signal_id=signal_id,
            market_id=market_id,
            error=str(exc),
        )


async def _load_active_l2_model():
    row = await fetchrow("SELECT model_payload FROM ml_meta_labeler WHERE is_active = true")
    if row is None:
        return None
    payload = row["model_payload"]
    payload_json = payload if isinstance(payload, str) else json.dumps(payload)
    return deserialize_from_json(payload_json)


def _meta_predict(
    examples: list[MetaLabelerExample],
    model,
) -> list[float]:
    if model is None or not examples:
        return [ex.calibrated_p for ex in examples]
    try:
        return [float(p) for p in model.predict(examples)]
    except Exception as exc:
        logger.warning("l2_predict_failed", error=str(exc), n=len(examples))
        return [ex.calibrated_p for ex in examples]


# Observability: считает свежие signals, которые INNER JOIN на detector_state отбросил
# (детектор не registered либо stage='shadow'/'archived'). Без этой телеметрии shadow-only
# детекторы выглядят как "black hole" — emit'ят signals, но execution_orders пустой.
# Один SQL под Postgres-таблицу detector_state, без side-effects.
_DROPPED_SIGNALS_TELEMETRY_SQL = """
SELECT ds.detector,
       COALESCE(dst.stage, 'unregistered') AS stage_or_missing,
       COUNT(*)                            AS n_dropped
  FROM detector_signals ds
  LEFT JOIN detector_state dst ON dst.detector = ds.detector
 WHERE ds.resolved = false
   AND ds.generated_at > now() - interval '6 hours'
   AND (dst.stage IS NULL OR dst.stage NOT IN ('virtual', 'live'))
 GROUP BY ds.detector, COALESCE(dst.stage, 'unregistered')
 ORDER BY n_dropped DESC
"""


async def _log_lifecycle_dropped_signals() -> None:
    """Считает свежие unresolved signals из детекторов вне ('virtual', 'live') stage.

    Покрывает silent-drop case (D44 emit'ит signals, JOIN их фильтрует, никакого лога).
    Логирует один раз на fetch; cheap aggregation query.
    """
    try:
        rows = await fetch(_DROPPED_SIGNALS_TELEMETRY_SQL)
    except Exception as exc:
        logger.warning("lifecycle_dropped_telemetry_failed", error=str(exc))
        return
    if not rows:
        return
    total = sum(int(r["n_dropped"]) for r in rows)
    by_detector = {f"{r['detector']}({r['stage_or_missing']})": int(r["n_dropped"]) for r in rows}
    logger.info(
        "lifecycle_dropped_signals",
        total=total,
        by_detector=by_detector,
        note="signals из shadow/unregistered детекторов не идут в v2_execution",
    )


async def _fetch_and_calibrate_signals(
    sql: str, *args: Any, after_fetch: Any = None, lock_timeout_ms: int | None = None
) -> list[dict[str, Any]]:
    """Общее ядро: fetch rows по переданному SQL + калибровка (L1/L2).

    Переиспользуется fetch_calibrated_detector_signals (список, execution
    cron) и fetch_calibrated_signal_by_id (один сигнал, execution-демон —
    Phase 3 Task 3, audit топ-7). Оба используют одну и ту же основу фильтров
    (_SIGNAL_SELECT_BASE_SQL) — здесь только общая пост-обработка строк, не
    сам SQL-текст (см. _FETCH_FRESH_SIGNALS_SQL / _FETCH_SIGNAL_BY_ID_SQL).

    after_fetch: опциональный async callback, вызывается сразу после fetch
    сигналов (сохраняет порядок fetch-вызовов как до рефакторинга — важно
    для тестов, мокающих fetch через side_effect-последовательность).

    lock_timeout_ms: быстрый лимит ожидания блокировки таблицы. Запрос джойнит
    markets, а VACUUM FULL markets (вс 04:00 UTC) держит ACCESS EXCLUSIVE lock
    ~2 мин — без лимита fetch висел до command_timeout=30s и ронял job по
    TimeoutError. При срабатывании PostgreSQL бросает QueryCanceledError —
    ловим и возвращаем [] (пустой тик, следующий cron-тик подхватит).
    """
    try:
        rows = await fetch(sql, *args, lock_timeout_ms=lock_timeout_ms)
    except asyncpg.exceptions.QueryCanceledError:
        # lock_timeout сработал — таблица под maintenance-локом (VACUUM FULL).
        # Пустой тик вместо падения job; следующий тик отработает после разлока.
        logger.warning("detector_signals_fetch_lock_timeout", lock_timeout_ms=lock_timeout_ms)
        return []
    if after_fetch is not None:
        await after_fetch()
    if not rows:
        return []

    cals = await _load_l1_calibrators()
    lgb_cals = await _load_lgb_calibrators()
    l2_model = await _load_active_l2_model()
    settings = get_settings()

    examples: list[MetaLabelerExample] = []
    enriched: list[dict[str, Any]] = []
    cal_source_counts: dict[str, int] = dict.fromkeys(CAL_SOURCES, 0)
    for r in rows:
        detector = r["detector"]
        raw_p = float(r["raw_p"])
        row_dict = dict(r)
        calibrated_p, cal_src = await _calibrate(
            raw_p, detector, cals, row=row_dict, lgb_cals=lgb_cals, settings=settings
        )
        cal_source_counts[cal_src] = cal_source_counts.get(cal_src, 0) + 1

        # Shadow-режим Задача 1.1: per-anchor калибровка логируется, но НЕ идёт
        # в calibrated_p/meta_p (торгуемое поведение не меняется).
        await _emit_cal_anchor_shadow(
            detector=detector,
            signal_id=str(r["id"]),
            market_id=r["market_id"],
            raw_p=raw_p,
            row=row_dict,
            cals=cals,
            lgb_cals=lgb_cals,
            settings=settings,
            flat_p=calibrated_p,
            flat_source=cal_src,
        )

        examples.append(
            MetaLabelerExample(
                detector_id=detector,
                calibrated_p=calibrated_p,
                edge_pct=float(r["expected_edge_pct"]),
                entry_price=float(r["entry_price"]),
                horizon_sec=int(r["horizon_sec"]),
                category=r.get("category"),
                volume_24h=float(r["volume_24h"] or 0),
                is_v2_post=r["generated_at"] > V2_CUTOVER,
                outcome=0,  # не используется при predict
                # Tier 3.1 — фичи должны совпадать со схемой fit (иначе модель
                # видит NaN там, где данные есть). alias hours_to_resolution_at_signal.
                hours_to_resolution=_opt_float(r.get("hours_to_resolution_at_signal")),
                recent_wr_30d=_opt_float(r.get("recent_wr_30d")),
                settle_velocity_hours=_opt_float(r.get("settle_velocity_hours")),
            )
        )

        enriched.append(
            {
                "detector_signal_id": str(r["id"]),
                "detector": detector,
                "market_id": r["market_id"],
                "event_id": r["event_id"],
                "side": r["side"],
                "raw_p": raw_p,
                "calibrated_p": calibrated_p,
                "cal_source": cal_src,  # "lgb" | "beta" | "raw"
                "meta_p": calibrated_p,  # обновим ниже
                "entry_price": float(r["entry_price"]),
                # Fix-3 (latency-аудит №3): current_price/yes_token_id нужны
                # sizing-телеметрии (v2_execution._sizing_current_price) —
                # без проброса точка 'sizing' в price_staleness_log молчала.
                # fair_price — TG-нотификация live-ордера. .get() — старые
                # моки/юнит-тесты собирают строки без этих полей.
                "current_price": _opt_float(r.get("current_price")),
                "yes_token_id": r.get("yes_token_id"),
                "fair_price": _opt_float(r.get("fair_price")),
                "expected_edge_pct": float(r["expected_edge_pct"]),
                "horizon_sec": int(r["horizon_sec"]),
                "evidence": r["evidence"],
                "generated_at": r["generated_at"],
                "category": r.get("category"),
                # temperature_blacklist гейт (v2_execution): без проброса гейт
                # молча не работает — тот же баг-класс, что Fix-3 выше.
                "question": r.get("question"),
                # jsonb приходит из asyncpg как str без set_type_codec — тот же
                # паттерн, что и v2_execution_resolve._settle_filled (fee_schedule
                # используется в _cost_drag_pct как dict, не str).
                "fee_schedule": parse_jsonb(r.get("fee_schedule")),
                "volume_24h": float(r["volume_24h"] or 0),
                "token_id": r["token_id"],
                "market_end_date": r.get("market_end_date"),
            }
        )

    if l2_model is not None and examples:
        meta_preds = _meta_predict(examples, l2_model)
        for sig, meta_p in zip(enriched, meta_preds, strict=True):
            sig["meta_p"] = meta_p
        logger.info(
            "detector_signals_calibrated",
            n=len(enriched),
            l1_fit=bool(cals),
            lgb_fit=bool(lgb_cals),
            l2_fit=True,
            cal_lgb=cal_source_counts["lgb"],
            cal_beta=cal_source_counts["beta"],
            cal_raw=cal_source_counts["raw"],
            cal_g18=cal_source_counts.get("g18_bin", 0),
        )
    else:
        logger.info(
            "detector_signals_calibrated",
            n=len(enriched),
            l1_fit=bool(cals),
            lgb_fit=bool(lgb_cals),
            l2_fit=False,
            cal_lgb=cal_source_counts["lgb"],
            cal_beta=cal_source_counts["beta"],
            cal_raw=cal_source_counts["raw"],
            cal_g18=cal_source_counts.get("g18_bin", 0),
        )

    return enriched


async def fetch_calibrated_detector_signals() -> list[dict[str, Any]]:
    """Возвращает свежие unresolved detector_signals с meta_p.

    Каждая dict содержит:
      detector, market_id, event_id, side, raw_p, calibrated_p, meta_p,
      entry_price, expected_edge_pct, horizon_sec, evidence, generated_at,
      category, volume_24h.

    Если L1/L2 не fit'ed — fallback к raw_p/calibrated_p (cold-start).
    """
    # Observability tick: телеметрия о signals, которые JOIN на detector_state
    # отбросил (shadow stage или unregistered detector — например D44).
    # after_fetch сохраняет исходный порядок fetch-вызовов: signals → telemetry
    # → calibrators (важно для тестов, мокающих fetch через side_effect).
    return await _fetch_and_calibrate_signals(
        _FETCH_FRESH_SIGNALS_SQL,
        after_fetch=_log_lifecycle_dropped_signals,
        lock_timeout_ms=_FETCH_LOCK_TIMEOUT_MS,
    )


async def fetch_calibrated_signal_by_id(signal_id: str) -> dict[str, Any] | None:
    """Возвращает один калиброванный сигнал по id — та же age/cooldown/stage
    фильтрация, что и fetch_calibrated_detector_signals (общая основа
    _SIGNAL_SELECT_BASE_SQL), плюс AND ds.id = $1.

    Используется execution-демоном (Phase 3 Task 3, audit топ-7): демон
    получает id из Redis-очереди сразу после insert сканом и обрабатывает
    сигнал за секунды, не дожидаясь минутного cron-тика. Если сигнал уже
    отфильтрован (протух/в cooldown/сменил stage) — возвращает None, демон
    молча пропускает (cron-watchdog всё равно подхватит, если применимо).
    """
    rows = await _fetch_and_calibrate_signals(_FETCH_SIGNAL_BY_ID_SQL, signal_id)
    return rows[0] if rows else None


async def fetch_signals_for_active_pipeline() -> list[dict[str, Any]]:
    """Router function — выбирает inference path по config.pipeline_version.

    Stage 4 / Two-Stage ML Pipeline.

    Default 'monolith' = legacy L2 path (fetch_calibrated_detector_signals).
    'v2_two_stage' = новый Layer A + Layer B path.

    Каждый path возвращает list of dicts с одинаковыми required keys:
        detector_signal_id, detector, market_id, event_id, side,
        raw_p, calibrated_p, meta_p, entry_price, expected_edge_pct,
        horizon_sec, evidence, generated_at, category, volume_24h, token_id.

    V2 path добавляет p_event, passed_layer_a_gate (опциональны для consumer).
    """
    from app.core.config import get_settings

    settings = get_settings()
    if settings.pipeline_version == "v2_two_stage":
        from app.services.two_stage_inference import fetch_two_stage_calibrated_signals

        return await fetch_two_stage_calibrated_signals()
    return await fetch_calibrated_detector_signals()
