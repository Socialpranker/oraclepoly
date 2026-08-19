# Methods Playbook — статистическая скамья

> §3.4 Phase 0 индексации базы знаний (`docs/specs/portfolio_analyst_agent.md`).
> Справочник методов ("когда Wilson, когда per-event bootstrap, когда changepoint,
> shrinkage на малых выборках"), не таблица механизмов рынка. Каждый раздел — короткое
> практическое правило + ссылки на реальные прогоны/ресёрчи проекта, где метод уже
> применялся. На этот файл ссылаются §3.1 и §4.2 спеки — используется как метод-справочник
> при синтезе аудитов, не переоткрывается заново каждый раз.

## Когда Wilson score interval

**Правило:** для доверительного интервала биномиальной пропорции (win rate, fill rate) на
малом/среднем N — Wilson, не Wald/normal-approximation. Wald ломается на N<30 и на p̂ близко
к 0/1 (даёт отрицательные нижние границы или интервалы шире [0,1]). Wilson устойчив в обоих
случаях и не требует scipy (замкнутая формула, только `math`).

**Где уже применяется в проекте:**
- `app/services/wilson_ci.py` — реализация (erfinv-приближение Winitzki 2008), обоснование
  в docstring со ссылкой на `research/adverse-selection-queue-position-2026-05/report.md §3`.
- `app/backtest/metrics.py::wilson_score_interval` — вторая независимая реализация (backtest
  framework), используется в per-category агрегации бэктеста (PR #459).
- `app/services/promote_gates.py` — mandatory promote gate использует Wilson lower bound
  для win-rate floor (`wilson_confidence`, `promote_gate_wilson_threshold_fill`).
- `d3-funnel-audit-2026-07-16` (memory) — directional accuracy D3 live отчитана как Wilson
  lower bound 61.9% на 34 сделках, не как raw winrate 38% — именно чтобы не переоценить
  малую выборку.

## Когда per-event (cluster) bootstrap, а не row-level split

**Правило:** для ROI confidence interval НЕЛЬЗЯ ресэмплировать строки (сделки) независимо —
несколько сделок на один и тот же event коррелированы (общий исход, общее движение рынка).
Правильная единица ресэмплинга — event, не row: сэмплировать events with replacement,
считать mean ROI per resample, брать перцентили по resample-распределению.

**Где уже применяется в проекте:**
- `app/calibration/validation.py::event_cluster_bootstrap_roi` — реализация (n_resamples=1000,
  percentile CI), используется в `validate_model()` как Gate 5 (`ci[0] > 0` = ROI значимо
  положительный).
- PR #694 (`validation-bootstrap-seed-flaky-ci-2026-07-12`, memory) — живой пример, почему
  seed обязателен: `event_cluster_bootstrap_roi` вызывался без `seed`, `default_rng(None)` брал
  энтропию ОС → нижняя граница CI гуляла вокруг нуля между прогонами, флейкал CI-гейт
  непостоянно. Фикс — `bootstrap_seed: int | None = 42` как параметр по умолчанию в
  `validate_model`, детерминированный lower=0.0062. Прод-fit джобы (`ml_event_selector_fit`,
  `ml_market_picker_fit`, `ml_meta_labeler_fit`) не передают seed → дефолт 42 действует и там.
- `scripts/research/audit_case4_g18_money_contribution.py` (Task 6, Phase 0 replay-кейс 4) —
  unit-тесты для bootstrap CI как часть контрфактического аудита G18 (walk-forward, sizing,
  bootstrap CI) — коммит `09166e81`.

## Когда changepoint detection

**Правило:** для онлайн-детекции момента, когда генеративные параметры временного ряда
резко меняются (whale-flow режим, ценовой режим рынка) — Bayesian online changepoint
detection дешевле полного пересчёта на каждом тике и не требует ретроспективной сегментации.

**Статус в проекте: researched, НЕ реализовано в production-коде.** Это честно
задокументированное ограничение, не пропуск — метод изучен как кандидат для конкретной
инженерной задачи (skip-unchanged-data оптимизация oracle-recalculate), но живой код,
использующий changepoint detection, в репозитории отсутствует (verified: нет совпадений
`changepoint` вне `research/`).

**Источники (researched, не production):**
- `research/archive/oracle-recalculate-optimization/sources/academic.json` — два академических
  источника по Bayesian online changepoint detection, явно помечены как "Directly applicable
  to skip-unchanged-data optimization: detect when input signals (whale txs, news, smart money)
  have changepoints, and only recalculate oracle score for markets with detected changes."
- `research/archive/oracle-recalculate-optimization/report.md` — рекомендация №1
  (инкрементальный пересчёт по эвристическому фильтру: новые whale-транзакции / цена
  сдвинулась >1% / новости) — задеплоенное решение фактически проще эвристического порога,
  не formal changepoint detector; сам changepoint остаётся неиспользованным заделом.
- Ближайший практический кандидат на будущее применение — `all-time-audit-hides-decaying-edge`
  (memory): edge детектора может затухать резко (D26: май +491.9% на 51 сигнале → июнь
  +2.7% на 43), сейчас это ловится только грубым monthly-бакетингом, не online changepoint.

## Когда shrinkage на малых выборках

**Правило:** сырое наблюдаемое значение бина/сегмента (win rate, calibration bin) с малым N
шумит непропорционально своему размеру выборки — на N=30 Wilson CI уже ±17пп. Shrinkage к
более широкому prior (по вышестоящей группе — категории, детектору) снижает дисперсию ценой
небольшого смещения; сила прайора (`k`) — эквивалент числа "виртуальных" наблюдений.

**Где уже применяется/исследуется в проекте:**
- `app/core/scoring.py::CATEGORY_KELLY_SHRINKAGE` — категорийный shrinkage Kelly-фракции
  (0.25-0.5×), обоснование в комментарии: "Baker & McHale 2013: shrinkage растёт с estimation
  variance" (Deep Research 2026-04-08). Живой прод-код (`get_kelly_shrinkage`).
- `scripts/research/g18_shrinkage_replay.py` — offline walk-forward replay, сравнивающий
  raw `wr_observed` бина против `wr_shrunk = (n·wr_bin + k·wr_prior) / (n + k)` (prior =
  кумулятивный WR на уровне detector×category) и против Wilson score midpoint. Гипотеза:
  shrinkage даёт лучший Brier на низких-n бинах (n=30 порог `MIN_OBS_FOR_LOOKUP`), где Wilson
  CI ±17пп напрямую шумит Kelly-стейк через EV sizer. Read-only реплей поверх прод-паттернов,
  не трогает `app/services/g18_bin_calibration.py`.
  Статус G18 в проекте: **включён на проде** (memory `fable-briefs-answers-2026-07-08.md`).
- Fractional Kelly (half/quarter-Kelly) как отраслевая практика — независимо подтверждена и
  практик-школой Thorp/MacLean/Ziemba (ошибка в оценке mean/edge доминирует над variance/
  covariance ошибками, importance ratio ~20:2:1), и отдельной теоретической школой
  (ergodicity economics, Ole Peters — оптимальное плечо из time-average, не ensemble-average
  роста капитала при мультипликативной динамике) — два независимых обоснования одного и
  того же практического вывода "сажать Kelly ниже full".
  `research/portfolio-analyst-agent-concept-gaps-2026-07/claims.csv` (C13 →
  `sources/31_maclean_thorp_ziemba_good_bad_kelly.md`; C14 →
  `sources/32_peters_optimal_leverage_nonergodicity.md`).
