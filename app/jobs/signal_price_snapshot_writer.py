"""Signal price snapshot writer — intraday yes_price для unresolved detector_signals.

Cron: каждые 5 минут через scripts/cron_runner.py signal-price-snapshot-writer.

Source of truth: markets.current_price (обновляется market-sync каждые 15 мин).
JOIN'имся detector_signals → markets через market_id (polymarket_id или condition_id).

Окно съёмки: пока signal unresolved И now() <= end_date + 6h буфер (с абсолютным
потолком 30 дней от generated_at). См. комментарий над _ELIGIBLE_WHERE — буфер
после конца рынка нужен чтобы захватить движение цены в момент резолва (это
самые информативные точки для realized-P&L decay-анализа в
scripts/research/signal_decay_study.py).

Idempotency: PRIMARY KEY (detector_signal_id, captured_at). Cron не может
дёрнуть дважды в одну секунду (асимметричный intervals), но защищаемся
через ON CONFLICT DO NOTHING.

Output: foundation для shadow TP/SL simulation
(research/shadow-gates-methodology-2026-05/2026-05-27_validation.md, H3 gap).

Storage estimate: ~10 живых detectors × ~150 unresolved signals × 288 snapshots/day ×
30 days × 60 bytes ≈ 750 MB worst-case. Retention двухуровневый:
  1. FK ON DELETE CASCADE — снапшоты удаляются при удалении самого detector_signal.
  2. CleanupRepository.delete_snapshots_for_resolved_signals (cron `cleanup`) —
     удаляет снапшоты сигналов, резолвнутых >7 дней назад (по resolved_at, чтобы
     не рвать траектории long-horizon сигналов для signal_decay_study.py). Без него
     таблица копила мёртвый груз resolved-сигналов (2886 MB на 2026-06-14).
"""

from __future__ import annotations

from typing import Any

import structlog

from app.services.database import execute, fetchval

logger = structlog.get_logger(__name__)


# INSERT current_price snapshot для всех unresolved signals.
#
# Окно съёмки (2026-05-29): снимаем пока signal unresolved И выполнено ЛИБО
# условие по горизонту (signal свежий), ЛИБО по концу рынка (рынок ещё не
# закрылся + буфер). Union двух bound'ов:
#
#   (A) generated_at > now() - horizon_sec × 2  — старый cap. Хорош для
#       short-horizon детекторов, пока сигнал «живой» по своему горизонту.
#   (B) now() <= end_date + 6h буфер            — новый. Держит съёмку
#       long-horizon сигналов (D36/D44 horizon 24h-7d) до фактического конца
#       рынка + буфер захватывает движение в момент резолва (самые
#       информативные точки для realized-P&L decay в signal_decay_study.py).
#
# Почему union, а не только (B): на проде ~1100 из 1280 unresolved сигналов
# уже прошли end_date (рынок закрылся, но hourly resolve-cron ещё не флипнул
# resolved). Чистое `now() <= end_date+6h` выкинуло бы их — coverage упал бы
# 753→168 (проверено SQL 2026-05-29). (A) их сохраняет, (B) добавляет
# long-horizon хвост, который (A) обрезал (D43 60→154, D38 31→123).
#
# Страховки от вечного накопления:
#   1. resolved = false — resolve-cron закрыл сигнал → съёмка стоп.
#   2. generated_at > now() - 30 дней — абсолютный потолок: зомби-сигнал
#      (рынок без end_date или resolve-cron завис) перестаёт сниматься через
#      30д независимо от (A)/(B). Безопасно: end_date >30д в будущем почти нет
#      (9 сигналов на проде).
#   - markets.current_price NOT NULL и строго в (0, 1) — на краю рынок закрыт,
#     ликвидности нет, цена unrealizable.
#
# ON CONFLICT DO NOTHING защищает от двойного запуска cron.
_ELIGIBLE_WHERE = """
 WHERE ds.resolved = false
   AND ds.generated_at > now() - interval '30 days'
   AND (
        ds.generated_at > now() - make_interval(secs => ds.horizon_sec * 2)
     OR now() <= COALESCE(m.end_date, ds.generated_at) + interval '6 hours'
   )
   AND m.current_price IS NOT NULL
   AND m.current_price > 0
   AND m.current_price < 1
"""

_SNAPSHOT_INSERT_SQL = f"""
INSERT INTO detector_signal_price_snapshots (detector_signal_id, captured_at, yes_price)
SELECT
    ds.id AS detector_signal_id,
    now() AS captured_at,
    m.current_price::numeric(8, 6) AS yes_price
  FROM detector_signals ds
  JOIN markets m
    ON m.polymarket_id = ds.market_id OR m.condition_id = ds.market_id
{_ELIGIBLE_WHERE}
ON CONFLICT (detector_signal_id, captured_at) DO NOTHING
"""

_COUNT_ELIGIBLE_SQL = f"""
SELECT COUNT(*)::bigint
  FROM detector_signals ds
  JOIN markets m
    ON m.polymarket_id = ds.market_id OR m.condition_id = ds.market_id
{_ELIGIBLE_WHERE}
"""


async def run_signal_price_snapshot_writer() -> dict[str, Any]:
    """Snapshot current yes_price для каждого unresolved detector_signal.

    Returns:
        {n_eligible, status, error?}
    """
    eligible = await fetchval(_COUNT_ELIGIBLE_SQL) or 0
    if eligible == 0:
        logger.info("signal_price_snapshot_no_unresolved_signals")
        return {"n_eligible": 0, "status": "no_signals"}

    try:
        await execute(_SNAPSHOT_INSERT_SQL)
    except Exception as exc:
        logger.error("signal_price_snapshot_insert_failed", error=str(exc))
        return {"n_eligible": int(eligible), "status": "error", "error": str(exc)}

    logger.info(
        "signal_price_snapshot_done",
        n_eligible=int(eligible),
    )
    return {"n_eligible": int(eligible), "status": "ok"}
