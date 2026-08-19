"""L2 нестационарность — эксперимент (research 2026-06-14, dry, НЕ пишет в БД).

Прогоняет ml-meta-labeler train с разными фильтрами обучающей выборки и печатает
validation-report (BSS, per-band AUC, ECE) БЕЗ записи в ml_meta_labeler. Цель —
проверить H1 (исключить 6 killed-детекторов) / H2 (узкое окно) / H3 (оба) /
H4 (H3 + регуляризация LGB) / H5 (H4 + recency-weighted training).

H5 — recency-weight: каждый обучающий пример взвешивается экспонентой по возрасту
(`w = 0.5 ** (age_days / half_life)`), свежие важнее. Бьёт в нестационарность данных
(weekly WR детекторов дрейфует). Наследует H4-фильтр (killed) + регуляризацию.

Usage (на проде через docker run --user 0 + --env-file .env, см. память
meta_labeler_tier31_features_2026-06-14):
    python scripts/research/l2_nonstat_experiment.py --exp baseline
    python scripts/research/l2_nonstat_experiment.py --exp h1
    python scripts/research/l2_nonstat_experiment.py --exp h2 --days 30
    python scripts/research/l2_nonstat_experiment.py --exp h3 --days 30
    python scripts/research/l2_nonstat_experiment.py --exp h4            # 30d, killed, reg
    python scripts/research/l2_nonstat_experiment.py --exp h5 --half-life 30   # + recency-weight
    python scripts/research/l2_nonstat_experiment.py --exp h6            # + regime-фичи (wr_drift/vol_7d)

H6 — прототип regime-фич БЕЗ правки прод meta_labeler.py: матрица собирается локально
(прод _vectorize + 2 колонки), raw LGB, тот же validation-gate. Реализовать в проде
(meta_labeler.py + ml_meta_labeler_fit) ТОЛЬКО если H6 даёт gate-pass.
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from pathlib import Path

# scripts/research/X.py → sys.path[0] = .../research, не видит app. Добавляем root.
sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

import lightgbm as lgb  # noqa: E402
import numpy as np  # noqa: E402

from app.calibration.meta_labeler import (  # noqa: E402
    MetaLabelerConfig,
    _vectorize,
    train_meta_labeler,
)
from app.jobs.ml_meta_labeler_fit import (
    HOLDOUT_FRACTION,
    MIN_TRAIN_SAMPLES,
    _load_l1_calibrators,
    _opt_float,
    _reproduce_holdout_split,
    _row_to_example,
    _validation_gate_check,
)
from app.services.database import close_db, fetch, init_db

# 6 детекторов, реально УДАЛЁННЫХ из кода (файлов нет) — чистый шум в обучении.
_KILLED = (
    "D5_long_horizon_extreme_fade",
    "D7b_no_favorite_fade",
    "D30_sports_favorite_fade",
    "D31_underdog_backing",
    "D32_extreme_tail_fade",
    "D33_general_favorite_fade",
)

# Базовый SQL = копия _FETCH_TRAINING_SQL из ml_meta_labeler_fit (с Tier 3.1), но с
# параметризуемыми (а) окном дней, (б) опциональным killed-фильтром.
_SQL_TEMPLATE = """
SELECT ds.detector,
       ds.confidence       AS raw_p,
       ds.expected_edge_pct,
       CASE WHEN ds.side = 'YES' THEN ds.current_price ELSE 1.0 - ds.current_price END AS entry_price,
       ds.horizon_sec,
       ds.generated_at,
       ds.outcome,
       ds.event_id,
       ds.event_size,
       m.category,
       m.volume_24h,
       CASE WHEN m.end_date IS NOT NULL
            THEN EXTRACT(epoch FROM (m.end_date - ds.generated_at)) / 3600.0
            ELSE NULL END    AS hours_to_resolution,
       rw.recent_wr_30d,
       sv.settle_velocity_hours{regime_select}
  FROM detector_signals ds
  LEFT JOIN markets m ON m.polymarket_id = ds.market_id
  LEFT JOIN LATERAL (
      SELECT AVG(CASE WHEN ds2.outcome = 'WIN' THEN 1.0 ELSE 0.0 END) AS recent_wr_30d
        FROM detector_signals ds2
       WHERE ds2.detector = ds.detector AND ds2.resolved = true
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
         AND eo.status = 'virtual_settled' AND eo.filled_at IS NOT NULL
         AND eo.settled_at IS NOT NULL AND eo.settled_at < ds.generated_at
         AND COALESCE((eo.meta->>'reentry_excluded')::bool, false) = false
  ) sv ON true{regime_lateral}
 WHERE ds.resolved = true
   AND ds.outcome IN ('WIN', 'LOSS')
   AND ds.generated_at > now() - make_interval(days => $1)
   {killed_filter}
 ORDER BY ds.generated_at ASC
"""

# H6 (regime-фичи) — добавляются в SELECT + LATERAL ТОЛЬКО для exp=h6, чтобы SQL
# H1–H5 остался байт-в-байт (воспроизводимость прошлых результатов).
# wr_drift = recent_wr_7d − recent_wr_30d (тренд режима детектора), считаем в Python.
# vol_7d = STDDEV дневной yes_price за 7д strictly ДО generated_at (leak-free).
_REGIME_SELECT = """,
       rw7.recent_wr_7d,
       vol.vol_7d"""

_REGIME_LATERAL = """
  LEFT JOIN LATERAL (
      SELECT AVG(CASE WHEN ds3.outcome = 'WIN' THEN 1.0 ELSE 0.0 END) AS recent_wr_7d
        FROM detector_signals ds3
       WHERE ds3.detector = ds.detector AND ds3.resolved = true
         AND ds3.outcome IN ('WIN', 'LOSS')
         AND ds3.generated_at < ds.generated_at
         AND ds3.generated_at >= ds.generated_at - interval '7 days'
  ) rw7 ON true
  LEFT JOIN LATERAL (
      SELECT STDDEV_POP(dps.yes_price) AS vol_7d
        FROM daily_price_snapshots dps
       WHERE dps.condition_id = ds.market_id
         AND dps.snapshot_date < CAST(ds.generated_at AS date)
         AND dps.snapshot_date >= CAST(ds.generated_at AS date) - 7
  ) vol ON true"""


def _make_config(reg: bool) -> MetaLabelerConfig:
    cfg = MetaLabelerConfig(min_train_samples=MIN_TRAIN_SAMPLES)
    if reg:
        # H4: регуляризация против overfit (train_brier 0.056 << holdout 0.171).
        cfg.hyperparams = {
            **cfg.hyperparams,
            "num_leaves": 15,
            "max_depth": 4,
            "min_data_in_leaf": 100,
            "lambda_l2": 5.0,
            "feature_fraction": 0.7,
        }
        cfg.num_boost_rounds = 120
    return cfg


def _split_indices(examples: list, holdout_fraction: float) -> tuple[list[int], list[int]]:
    """Индексный аналог _event_aware_split — тот же детерминированный chronological
    event-level split, но возвращает (train_idx, holdout_idx) вместо объектов.
    Нужен, чтобы синхронно делить расширенную (с regime-фичами) матрицу X И examples.
    Логика mirror'ит app.calibration.meta_labeler._event_aware_split байт-в-байт.
    """
    n = len(examples)
    n_with_event = sum(1 for ex in examples if ex.event_id)
    if n_with_event < n * 0.5:
        cut = int(n * (1.0 - holdout_fraction))
        return list(range(cut)), list(range(cut, n))
    first_seen: dict[str, int] = {}
    eids: list[str] = []
    for idx, ex in enumerate(examples):
        eid = ex.event_id if ex.event_id else f"__no_event__{idx}"
        eids.append(eid)
        if eid not in first_seen:
            first_seen[eid] = idx
    sorted_events = sorted(first_seen.keys(), key=lambda e: first_seen[e])
    cut_event = int(len(sorted_events) * (1.0 - holdout_fraction))
    train_events = set(sorted_events[:cut_event])
    train_idx = [i for i, eid in enumerate(eids) if eid in train_events]
    holdout_idx = [i for i, eid in enumerate(eids) if eid not in train_events]
    return train_idx, holdout_idx


def _run_h6_regime(rows: list, examples: list, reg: bool) -> tuple[bool, list, float, float]:
    """H6: прототип regime-фич БЕЗ правки прод-кода.

    Собирает матрицу X = прод _vectorize(examples) + 2 доп-колонки (wr_drift, vol_7d),
    обучает raw LGB (те же гиперпараметры, что H4), гоняет ТОТ ЖЕ _validation_gate_check
    на holdout. Прод meta_labeler.py не трогается — фичи живут только здесь.

    Возвращает (gate_pass, failures, train_brier, holdout_brier).
    """
    # 1) Базовая матрица — ровно как в проде (KNOWN_* one-hot + _BASE_FEATURES).
    x_base, y, base_names = _vectorize(examples)

    # 2) Доп-колонки regime, выровненные по индексу rows == examples.
    #    wr_drift = recent_wr_7d − recent_wr_30d (None если хоть один отсутствует → NaN).
    #    vol_7d — STDDEV дневной цены (NaN если нет снапшотов: ранние сигналы / новый рынок).
    wr_drift = np.full(len(rows), np.nan, dtype=np.float64)
    vol_7d = np.full(len(rows), np.nan, dtype=np.float64)
    for i, r in enumerate(rows):
        wr7 = _opt_float(r.get("recent_wr_7d"))
        wr30 = _opt_float(r.get("recent_wr_30d"))
        if wr7 is not None and wr30 is not None:
            wr_drift[i] = wr7 - wr30
        v = _opt_float(r.get("vol_7d"))
        if v is not None:
            vol_7d[i] = v
    n_drift = int(np.sum(~np.isnan(wr_drift)))
    n_vol = int(np.sum(~np.isnan(vol_7d)))
    print(
        f"  regime coverage: wr_drift {n_drift}/{len(rows)} non-null, "
        f"vol_7d {n_vol}/{len(rows)} non-null",
        flush=True,
    )

    x_full = np.column_stack([x_base, wr_drift, vol_7d])
    feature_names = list(base_names) + ["wr_drift", "vol_7d"]

    # 3) Индексный split — идентичный train_meta_labeler/_reproduce_holdout_split.
    train_idx, holdout_idx = _split_indices(examples, HOLDOUT_FRACTION)
    x_train, y_train = x_full[train_idx], y[train_idx]
    x_holdout, y_holdout = x_full[holdout_idx], y[holdout_idx]

    # 4) Raw LGB с теми же гиперпараметрами, что H4 (через _make_config).
    cfg = _make_config(reg)
    train_set = lgb.Dataset(x_train, label=y_train, feature_name=feature_names)
    valid_set = lgb.Dataset(x_holdout, label=y_holdout, reference=train_set)
    callbacks = []
    if cfg.early_stopping_rounds:
        callbacks.append(lgb.early_stopping(cfg.early_stopping_rounds, verbose=False))
    callbacks.append(lgb.log_evaluation(period=0))
    booster = lgb.train(
        cfg.hyperparams, train_set, num_boost_round=cfg.num_boost_rounds,
        valid_sets=[train_set, valid_set], callbacks=callbacks,
    )

    # 5) Тот же gate на holdout. holdout_examples по тем же индексам, что x_holdout.
    holdout_examples = [examples[i] for i in holdout_idx]
    preds = [float(p) for p in booster.predict(x_holdout, num_iteration=booster.best_iteration)]
    train_pred = booster.predict(x_train, num_iteration=booster.best_iteration)
    train_brier = float(np.mean((np.asarray(train_pred) - y_train) ** 2))
    holdout_brier = float(np.mean((np.asarray(preds) - y_holdout) ** 2))
    passes, failures = _validation_gate_check(holdout_examples, preds, name="exp_h6")
    # importance — увидеть, берёт ли модель новые фичи вообще.
    imp = dict(zip(feature_names, booster.feature_importance(importance_type="gain")))
    print(
        f"  regime importance(gain): wr_drift={imp.get('wr_drift', 0):.0f} "
        f"vol_7d={imp.get('vol_7d', 0):.0f} calibrated_p={imp.get('calibrated_p', 0):.0f}",
        flush=True,
    )
    return passes, failures, train_brier, holdout_brier


def _recency_weights(rows: list, half_life_days: float) -> list[float]:
    """Экспоненциальный вес по возрасту примера: свежие важнее.

    w_i = 0.5 ** (age_days_i / half_life), где age = (max(generated_at) - generated_at_i).
    Самый свежий пример → 1.0; через один half-life → 0.5. Порядок СОВПАДАЕТ с rows
    (а значит и с examples — оба строятся из одного rows в одном порядке), это критично:
    train_meta_labeler раздаёт sample_weight по индексам исходного массива.
    """
    ts = [r["generated_at"] for r in rows]
    newest = max(ts)
    out = []
    for t in ts:
        age_days = (newest - t).total_seconds() / 86400.0
        out.append(0.5 ** (age_days / half_life_days))
    return out


async def run(exp: str, days: int, reg: bool = False, half_life: float = 30.0) -> None:
    killed_filter = ""
    if exp in ("h1", "h3", "h4", "h5", "h6"):
        names = ", ".join(f"'{d}'" for d in _KILLED)
        killed_filter = f"AND ds.detector NOT IN ({names})"
    # regime-фичи в SQL — только для h6 (иначе H1–H5 SQL остаётся байт-в-байт).
    regime_select = _REGIME_SELECT if exp == "h6" else ""
    regime_lateral = _REGIME_LATERAL if exp == "h6" else ""
    sql = _SQL_TEMPLATE.format(
        killed_filter=killed_filter,
        regime_select=regime_select,
        regime_lateral=regime_lateral,
    )

    await init_db(min_size=1, max_size=3)
    try:
        rows = await fetch(sql, days)
        print(f"[exp={exp} days={days} reg={reg}] rows={len(rows)}", flush=True)
        if len(rows) < MIN_TRAIN_SAMPLES:
            print(f"  too few: {len(rows)} < {MIN_TRAIN_SAMPLES}", flush=True)
            return
        cals = await _load_l1_calibrators()
        examples = [_row_to_example(dict(r), cals) for r in rows]

        # H6: regime-фичи (wr_drift + vol_7d) через локальный прототип, БЕЗ правки прод-кода.
        if exp == "h6":
            passes, failures, train_brier, holdout_brier = _run_h6_regime(rows, examples, reg)
            n_holdout = len(_split_indices(examples, HOLDOUT_FRACTION)[1])
            print(
                f"  RESULT exp={exp} days={days}: n_train={len(examples) - n_holdout} "
                f"n_holdout={n_holdout} train_brier={train_brier:.4f} "
                f"holdout_brier={holdout_brier:.4f} GATE_PASS={passes}",
                flush=True,
            )
            if failures:
                print(f"  failures: {failures}", flush=True)
            return

        # H5: recency-weight. Вес считаем ИЗ rows (generated_at), т.к. MetaLabelerExample
        # его не хранит. Порядок rows == порядок examples == то, что ждёт sample_weight.
        sample_weight = None
        if exp == "h5":
            sample_weight = _recency_weights(rows, half_life)
            print(
                f"  recency-weight half_life={half_life}d: "
                f"w min={min(sample_weight):.4f} max={max(sample_weight):.4f} "
                f"(oldest example weighted {min(sample_weight):.4f})",
                flush=True,
            )
        model = train_meta_labeler(
            examples, config=_make_config(reg),
            holdout_fraction=HOLDOUT_FRACTION,
            sample_weight=sample_weight,
        )
        holdout = _reproduce_holdout_split(examples, HOLDOUT_FRACTION)
        preds = [float(p) for p in model.predict(holdout)] if holdout else []
        passes, failures = _validation_gate_check(holdout, preds, name=f"exp_{exp}")
        print(
            f"  RESULT exp={exp} days={days}: n_train={model.n_train} n_holdout={model.n_holdout} "
            f"train_brier={model.train_brier:.4f} holdout_brier={model.holdout_brier:.4f} "
            f"GATE_PASS={passes}",
            flush=True,
        )
        if failures:
            print(f"  failures: {failures}", flush=True)
    finally:
        await close_db()


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--exp", choices=["baseline", "h1", "h2", "h3", "h4", "h5", "h6"], required=True)
    ap.add_argument("--days", type=int, default=None)
    ap.add_argument("--half-life", type=float, default=30.0,
                    help="H5 only: recency-weight half-life в днях (default 30)")
    args = ap.parse_args()
    # h1/h3/h4/h5/h6 → killed-фильтр; h2/baseline → все детекторы.
    # Окно по умолчанию: h2..h6 строятся на узком 30d (H3-база), baseline/h1 — 90d.
    # --days переопределяет.
    default_days = 30 if args.exp in ("h2", "h3", "h4", "h5", "h6") else 90
    days = args.days if args.days is not None else default_days
    # H4/H5/H6 наследуют регуляризацию LGB против overfit (train_brier << holdout).
    # H5 — recency-weight; H6 — regime-фичи (wr_drift + vol_7d).
    asyncio.run(run(args.exp, days, reg=(args.exp in ("h4", "h5", "h6")), half_life=args.half_life))


if __name__ == "__main__":
    main()
