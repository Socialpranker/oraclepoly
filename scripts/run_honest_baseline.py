"""CLI runner для honest baseline (Stage 1, Two-Stage ML Pipeline).

Запускается ВРУЧНУЮ на проде через SSH (НЕ cron). One-shot.

Usage:
    docker exec -i oraclepoly-backend python scripts/run_honest_baseline.py

Output: JSON с числами для memory file (memory/honest_baseline_<date>.md).
Записывает модель в ml_meta_labeler с is_active=false (для возможного inspection).

Pre-requisites (Stage 0):
- detector_signals.event_size, archetype populated
- mv_event_outcome refreshed
"""

from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path

# Add project root to path (analogous to scripts/cron_runner.py pattern)
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.core.cache import close_redis, init_redis  # noqa: E402
from app.core.config import get_settings  # noqa: E402
from app.jobs.ml_honest_baseline_fit import run_ml_honest_baseline_fit  # noqa: E402
from app.services.database import close_db, init_db  # noqa: E402


async def _main() -> None:
    settings = get_settings()
    await init_db(min_size=1, max_size=4)
    await init_redis(settings.redis_url)
    try:
        result = await run_ml_honest_baseline_fit()
    finally:
        await close_db()
        await close_redis()
    print(json.dumps(result, indent=2, default=str))


if __name__ == "__main__":
    asyncio.run(_main())
