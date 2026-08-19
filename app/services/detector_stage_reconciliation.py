"""Reconciliation backtest→shadow→virtual per-detector — гипотеза H3.

Контекст (research/virtual-feedback-2026-05/report.md):

Детектор проходит стадии 🧪 Backtest → 👁 Shadow → 💸 Virtual → 🔴 Live, и на
КАЖДОЙ стадии источник правды разный (см. [[shadow-roi-virtual-pnl]]):
  - backtest-projection: `docs/backtests/{detector}.md` frontmatter `roi_net_pct`
    + РУЧНОЙ `honest_haircut_factor`.
  - shadow: `detector_signals.realized_roi_net` (hold-to-resolution).
  - virtual: `execution_orders.net_pnl` (TP/SL exit), status='virtual_settled'.

Известная боль (см. [[backtest-wallet-d14-keeper-d34-deadend]]): canonical-backtest
СИСТЕМАТИЧЕСКИ переоценивает реальность (D44/D5/D31 были backtest-KEEPER, но прод
убыточен). Сейчас расхождение ловится РУЧНЫМ haircut в docs. H3 — собрать три
источника per-detector на одну строку и посчитать divergence автоматически,
чтобы (а) видеть дрейф без ручного аудита, (б) дать данные для пересчёта haircut.

ВАЖНО (границы H3):
  - Это ДИАГНОСТИКА расхождения, а НЕ авто-применение. haircut НЕ переписывается
    автоматически — рекомендация для человека.
  - Sample-gate N<30 → "insufficient_data", divergence не считается (шум).
  - Отличается от `execution_reconcile.py` (broker-state: orphan orders, kill-switch).
    Здесь — PERFORMANCE reconciliation между стадиями жизненного цикла.

Pure core (`compute_divergence`, `reconcile_detector`) тестируем на синтетике без БД.
Тонкие readers (frontmatter + SQL) — отдельно, side-effect только read.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

# Sample-gate: ниже этого N virtual settled — шум, divergence не считаем.
# Независимый порог reconciliation (demote v4 перешёл на signal-level
# _SAMPLE_SIZE_THRESHOLD=40, см. [[shadow-roi-virtual-pnl]]).
MIN_N_SETTLED: int = 30

_FRONTMATTER_RE = re.compile(r"^---\s*\n(.*?)\n---\s*\n", re.DOTALL)


@dataclass(frozen=True)
class DetectorSources:
    """Три источника правды по одному детектору на момент сверки.

    Любое поле может быть None, если источник пуст (детектор не в стадии,
    нет backtest-дока и т.п.).
    """

    detector: str
    # backtest-projection: gross net ROI % и ручной haircut [0..1].
    backtest_roi_net_pct: float | None
    backtest_haircut_factor: float | None
    # shadow: средний realized_roi_net и N сигналов. ХРАНИТСЯ В ПРОЦЕНТАХ
    # (net_pnl/notional*100, см. detector_signals_resolve._realized_roi_net_pct):
    # 4.0 = +4%, НЕ доля. Сопоставимо с backtest_roi_net_pct без конверсии.
    shadow_mean_roi_net: float | None
    shadow_n_signals: int
    # virtual: суммарный и средний net_pnl (USD) и N settled.
    virtual_sum_pnl_usd: float | None
    virtual_mean_pnl_usd: float | None
    virtual_n_settled: int


@dataclass(frozen=True)
class ReconcileRow:
    """Результат сверки одного детектора — одна строка отчёта."""

    detector: str
    status: str  # 'ok' | 'insufficient_data'
    reason: str | None
    # Проекции, приведённые к сопоставимому виду (ROI % per signal).
    backtest_honest_roi_pct: float | None  # roi_net × haircut
    shadow_roi_pct: float | None  # realized_roi_net (уже в %)
    virtual_mean_pnl_usd: float | None
    virtual_n_settled: int
    # Divergence: насколько backtest-honest переоценивает shadow (в пунктах ROI%).
    # >0 → backtest оптимистичнее реальности (систематическая боль проекта).
    backtest_vs_shadow_divergence_pct: float | None
    # Подсказка для пересчёта haircut: какой haircut выровнял бы backtest к shadow.
    implied_haircut_factor: float | None


def compute_divergence(src: DetectorSources) -> ReconcileRow:
    """Pure: три источника → строка сверки с divergence. Без БД, без side-effects.

    Sample-gate: virtual_n_settled < MIN_N_SETTLED → insufficient_data
    (divergence не считаем, см. [[shadow-roi-virtual-pnl]] — N<30 шум).
    """
    if src.virtual_n_settled < MIN_N_SETTLED:
        return ReconcileRow(
            detector=src.detector,
            status="insufficient_data",
            reason=f"virtual_n_settled={src.virtual_n_settled} < {MIN_N_SETTLED}",
            backtest_honest_roi_pct=None,
            shadow_roi_pct=None,
            virtual_mean_pnl_usd=src.virtual_mean_pnl_usd,
            virtual_n_settled=src.virtual_n_settled,
            backtest_vs_shadow_divergence_pct=None,
            implied_haircut_factor=None,
        )

    # backtest honest = gross ROI × ручной haircut (если оба есть).
    backtest_honest: float | None = None
    if src.backtest_roi_net_pct is not None and src.backtest_haircut_factor is not None:
        backtest_honest = src.backtest_roi_net_pct * src.backtest_haircut_factor

    # shadow ROI уже в процентах (realized_roi_net = net_pnl/notional*100) —
    # сопоставимо с backtest_roi_net_pct БЕЗ домножения (fix 2026-05-29).
    shadow_roi_pct = src.shadow_mean_roi_net if src.shadow_mean_roi_net is not None else None

    divergence: float | None = None
    implied_haircut: float | None = None
    if backtest_honest is not None and shadow_roi_pct is not None:
        divergence = backtest_honest - shadow_roi_pct
        # implied haircut: какой множитель привёл бы gross backtest к shadow.
        if src.backtest_roi_net_pct not in (None, 0.0):
            implied_haircut = shadow_roi_pct / src.backtest_roi_net_pct

    return ReconcileRow(
        detector=src.detector,
        status="ok",
        reason=None,
        backtest_honest_roi_pct=round(backtest_honest, 2) if backtest_honest is not None else None,
        shadow_roi_pct=round(shadow_roi_pct, 2) if shadow_roi_pct is not None else None,
        virtual_mean_pnl_usd=src.virtual_mean_pnl_usd,
        virtual_n_settled=src.virtual_n_settled,
        backtest_vs_shadow_divergence_pct=round(divergence, 2) if divergence is not None else None,
        implied_haircut_factor=round(implied_haircut, 3) if implied_haircut is not None else None,
    )


def reconcile_detectors(sources: list[DetectorSources]) -> list[ReconcileRow]:
    """Pure: список источников → список строк сверки. Тестируемо на синтетике."""
    return [compute_divergence(s) for s in sources]


# ─── Thin readers (side-effect: read-only) ───────────────────────────────────


def _parse_frontmatter(text: str) -> dict[str, str]:
    """Минимальный YAML-frontmatter парсер (key: value, без вложенности).

    Не тащим PyYAML ради десятка скаляров. Берём только верхнеуровневые
    `key: value` строки внутри первого `---...---` блока.
    """
    m = _FRONTMATTER_RE.search(text)
    if not m:
        return {}
    out: dict[str, str] = {}
    for line in m.group(1).splitlines():
        if ":" not in line or line.lstrip().startswith("#"):
            continue
        key, _, value = line.partition(":")
        out[key.strip()] = value.strip()
    return out


def read_backtest_projection(detector: str, docs_dir: Path) -> tuple[float | None, float | None]:
    """Прочитать (roi_net_pct, honest_haircut_factor) из docs/backtests/{detector}.md.

    Файл может называться `{detector}.md` или `{detector}_*.md` (как
    D3_cross_mkt_arb.md). Возвращает (None, None) если файл не найден/нет полей.
    """
    candidates = list(docs_dir.glob(f"{detector}.md")) + list(docs_dir.glob(f"{detector}_*.md"))
    if not candidates:
        return None, None
    fm = _parse_frontmatter(candidates[0].read_text(encoding="utf-8"))

    def _num(key: str) -> float | None:
        raw = fm.get(key)
        if raw is None:
            return None
        try:
            return float(raw)
        except ValueError:
            return None

    return _num("roi_net_pct"), _num("honest_haircut_factor")


async def fetch_shadow_virtual_metrics(detector: str) -> dict[str, Any]:
    """Read-only: shadow realized_roi_net + virtual net_pnl per detector из БД.

    Фильтрует execution-баг VOIDED_BOOK_ORDERING_BUG (net_pnl=0, не реальный exit —
    см. journal H0). Возвращает dict с shadow/virtual агрегатами.

    Импорт database внутри функции: pure core (compute_divergence) тестируется
    без asyncpg, тяжёлый импорт нужен только на реальном проходе.
    """
    from app.services.database import fetch  # noqa: PLC0415

    shadow_rows = await fetch(
        """
        SELECT AVG(realized_roi_net) AS mean_roi, COUNT(*) AS n
        FROM detector_signals
        WHERE detector = $1 AND realized_roi_net IS NOT NULL
        """,
        detector,
    )
    virtual_rows = await fetch(
        """
        SELECT SUM(net_pnl) AS sum_pnl, AVG(net_pnl) AS mean_pnl, COUNT(*) AS n
        FROM execution_orders
        WHERE detector = $1
          AND status = 'virtual_settled'
          AND (exit_reason IS NULL OR exit_reason NOT ILIKE '%VOIDED%')
        """,
        detector,
    )
    s = shadow_rows[0] if shadow_rows else {}
    v = virtual_rows[0] if virtual_rows else {}
    return {
        "shadow_mean_roi_net": float(s["mean_roi"]) if s.get("mean_roi") is not None else None,
        "shadow_n_signals": int(s.get("n") or 0),
        "virtual_sum_pnl_usd": float(v["sum_pnl"]) if v.get("sum_pnl") is not None else None,
        "virtual_mean_pnl_usd": float(v["mean_pnl"]) if v.get("mean_pnl") is not None else None,
        "virtual_n_settled": int(v.get("n") or 0),
    }


async def build_sources(detectors: list[str], docs_dir: Path) -> list[DetectorSources]:
    """Собрать DetectorSources для списка детекторов (read-only БД + docs).

    Это единственная функция с реальными side-effects (read). Чистый расчёт —
    через reconcile_detectors() поверх результата.
    """
    out: list[DetectorSources] = []
    for det in detectors:
        roi, haircut = read_backtest_projection(det, docs_dir)
        m = await fetch_shadow_virtual_metrics(det)
        out.append(
            DetectorSources(
                detector=det,
                backtest_roi_net_pct=roi,
                backtest_haircut_factor=haircut,
                shadow_mean_roi_net=m["shadow_mean_roi_net"],
                shadow_n_signals=m["shadow_n_signals"],
                virtual_sum_pnl_usd=m["virtual_sum_pnl_usd"],
                virtual_mean_pnl_usd=m["virtual_mean_pnl_usd"],
                virtual_n_settled=m["virtual_n_settled"],
            )
        )
    return out
