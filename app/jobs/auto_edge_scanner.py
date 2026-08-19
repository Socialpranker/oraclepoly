"""Auto Edge Scanner — cron-runner shim.

Production scanner runs on host venv-research (needs /mnt/data + DuckDB + scipy).
This shim allows manual `docker exec ... cron_runner.py auto-edge-scan` triggering
by spawning a subprocess via host SSH from the container, mirroring the pattern
used in refresh_wallet_smart_money.

In production, the scheduled execution path is the direct host cron line in
crontab.conf (host venv-research), NOT this shim. The shim exists for:
  - cron_runner.py registry consistency
  - manual testing via docker exec
"""

from __future__ import annotations

from typing import Any

import structlog

logger = structlog.get_logger()


async def run_auto_edge_scanner() -> dict[str, Any]:
    """Cron-runner entry point. Logs guidance — actual scan runs on host."""
    logger.info(
        "auto_edge_scanner_shim_called",
        message=(
            "Production scanner runs on host venv-research via crontab.conf "
            "(0 3 * * 0). Manual trigger: ssh root@VPS 'cd /root/oracle && "
            "/root/oracle/.venv-research/bin/python scripts/auto_edge_scanner.py'."
        ),
    )
    return {
        "status": "shim_only",
        "message": "Production scanner runs on host venv-research; see crontab.conf",
        "manual_trigger": "/root/oracle/.venv-research/bin/python scripts/auto_edge_scanner.py",
    }
