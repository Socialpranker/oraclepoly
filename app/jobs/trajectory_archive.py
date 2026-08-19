"""Архивация траекторий цен зарезолвленных сигналов перед их удалением cleanup'ом.

detector_signal_price_snapshots — единственный источник полного пути цены сигнала
от входа до резолва; cleanup стирает его через 7 дней после резолва. Для exit-policy
ресёрча (MAE/MFE, counterfactual TP/SL) эти пути — основной актив, поэтому перед
удалением дописываем уходящие строки в дневной parquet.

Куда: research/data/trajectory_archive/snapshots_YYYY-MM-DD.parquet. В контейнере
это /app/research/data → хостовый /root/oracle/research/data (mount уже есть,
docker-compose не трогаем). ~30-60 MB/день.

Предикат выборки обязан совпадать с delete_snapshots_for_resolved_signals
(cleanup_repository.py): ds.resolved=true AND ds.resolved_at < now()-days.
Метаданные сигнала не дублируем — detector_signals не удаляются, join при анализе.

Список кандидатов на архивацию берётся ТОЛЬКО из detector_signals (без JOIN на
25M-строчную таблицу снапшотов — такой JOIN однажды упрётся в pool
command_timeout=30s). Это возвращает id всех старых зарезолвленных сигналов
(~200K), большинство уже без снапшотов — их per-chunk fetch просто вернёт 0
строк и будет пропущен. Фильтр "снапшоты ещё существуют" происходит естественно
на этапе per-chunk fetch, а не на этапе отбора id.

Дубли между соседними дневными файлами возможны (архив успел, удаление упало —
завтра те же строки уйдут в новый файл): при чтении архива дедупить по
(detector_signal_id, captured_at).
"""

from __future__ import annotations

import uuid
from contextlib import suppress
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pyarrow as pa
import pyarrow.parquet as pq
import structlog

from app.services.database import fetch

logger = structlog.get_logger()

ARCHIVE_DIR = Path("research/data/trajectory_archive")

# Батч сигналов на один fetch снапшотов: ~450 точек/сигнал → ~200K строк на батч,
# держит память плоской при ~2M уходящих строк за прогон.
SIGNALS_PER_BATCH = 500

SCHEMA = pa.schema(
    [
        ("detector_signal_id", pa.string()),
        ("captured_at", pa.timestamp("us", tz="UTC")),
        ("yes_price", pa.float64()),
    ]
)

STALE_TMP_AGE = timedelta(hours=24)


def _sweep_stale_tmp_files() -> None:
    """Удалить .tmp-файлы старше 24ч — остатки прогонов, упавших до atomic rename.

    Свежие .tmp не трогаем — могут принадлежать конкурентному прогону.
    Best-effort: ошибка на отдельном файле не должна ронять архивацию.
    """
    now = datetime.now(UTC).timestamp()
    removed = 0
    for stale in ARCHIVE_DIR.glob("*.tmp"):
        try:
            if now - stale.stat().st_mtime > STALE_TMP_AGE.total_seconds():
                stale.unlink(missing_ok=True)
                removed += 1
        except OSError:
            continue
    if removed:
        logger.info("trajectory_archive_stale_tmp_removed", count=removed)


async def archive_resolved_trajectories(days: int = 7, run_date: str | None = None) -> dict[str, Any]:
    """Записать в parquet снапшоты сигналов, зарезолвленных >days назад.

    Returns:
        {ok, rows, file} — ok=False сигналит cleanup'у пропустить удаление.
    """
    cutoff = datetime.now(UTC) - timedelta(days=days)
    date_str = run_date or datetime.now(UTC).strftime("%Y-%m-%d")
    final_path = ARCHIVE_DIR / f"snapshots_{date_str}.parquet"
    tmp_path = final_path.parent / f"{final_path.name}.{uuid.uuid4().hex[:8]}.tmp"

    try:
        # mkdir внутри try: PermissionError на root-owned mount (инцидент 2026-07-06)
        # должен вернуть ok=False, а не пролететь в cleanup и убить остальные шаги.
        ARCHIVE_DIR.mkdir(parents=True, exist_ok=True)
        _sweep_stale_tmp_files()

        signal_ids = [
            r["id"]
            for r in await fetch(
                """
                SELECT id
                FROM detector_signals
                WHERE resolved = true AND resolved_at < $1
                """,
                cutoff,
            )
        ]
        if not signal_ids:
            logger.info("trajectory_archive_nothing_to_archive", days=days)
            return {"ok": True, "rows": 0, "file": None}

        total = 0
        writer = pq.ParquetWriter(tmp_path, SCHEMA, compression="zstd")
        try:
            for i in range(0, len(signal_ids), SIGNALS_PER_BATCH):
                chunk = signal_ids[i : i + SIGNALS_PER_BATCH]
                rows = await fetch(
                    """
                    SELECT detector_signal_id, captured_at, yes_price
                    FROM detector_signal_price_snapshots
                    WHERE detector_signal_id = ANY($1)
                    ORDER BY detector_signal_id, captured_at
                    """,
                    chunk,
                )
                if not rows:
                    continue
                writer.write_table(
                    pa.table(
                        {
                            "detector_signal_id": [str(r["detector_signal_id"]) for r in rows],
                            "captured_at": [r["captured_at"] for r in rows],
                            "yes_price": [float(r["yes_price"]) for r in rows],
                        },
                        schema=SCHEMA,
                    )
                )
                total += len(rows)
        finally:
            writer.close()

        if total == 0:
            tmp_path.unlink(missing_ok=True)
            logger.info("trajectory_archive_no_rows_for_signals", signals=len(signal_ids))
            return {"ok": True, "rows": 0, "file": None}

        tmp_path.rename(final_path)
        logger.info("trajectory_archive_done", rows=total, signals=len(signal_ids), file=str(final_path))
        return {"ok": True, "rows": total, "file": str(final_path)}
    except Exception as e:
        # suppress: если сам ARCHIVE_DIR недоступен/не каталог, unlink тоже кидает
        with suppress(OSError):
            tmp_path.unlink(missing_ok=True)
        logger.error("trajectory_archive_failed", error=str(e))
        return {"ok": False, "rows": 0, "file": None}
