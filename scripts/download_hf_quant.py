#!/usr/bin/env python3
"""Manual download HF quant.parquet (safe).

Зачем manual: 36GB файл, на 79% disk это рискованно качать в фоне.
Запускается человеком после TG alert от hf-upstream-check.

Usage:
    python3 scripts/download_hf_quant.py --confirm
    python3 scripts/download_hf_quant.py --dry-run  # только проверить размер

После download надо:
1. Проверить схему: `python3 -c "import duckdb; print(duckdb.sql(...))"`
2. Запустить ETL rebuild (если совместимо со старой схемой)
3. Refit L1 calibrators: `cron_runner.py ml-calibrator-fit`
4. Refit L2: `cron_runner.py ml-meta-labeler-fit`
"""

from __future__ import annotations

import argparse
import os
import shutil
import sys
import time
from pathlib import Path

QUANT_LOCAL = "/mnt/data/hf_data/quant.parquet"
HF_REPO = "SII-WANGZJ/Polymarket_data"
HF_FILENAME = "quant.parquet"
BACKUP_PATH = "/mnt/data/hf_data/quant.parquet.bak"


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--confirm", action="store_true", help="Подтвердить download (иначе dry-run)")
    p.add_argument("--no-backup", action="store_true", help="Не создавать .bak файл")
    args = p.parse_args()

    try:
        from huggingface_hub import HfApi, hf_hub_download
    except ImportError:
        print("ERROR: huggingface_hub не установлен. pip install huggingface_hub")
        return 1

    api = HfApi()
    info = api.dataset_info(HF_REPO)
    upstream_mtime = info.last_modified

    local = Path(QUANT_LOCAL)
    if local.exists():
        local_size_gb = local.stat().st_size / 1024**3
        local_mtime = time.ctime(local.stat().st_mtime)
        print(f"Local: {QUANT_LOCAL}")
        print(f"  size: {local_size_gb:.1f} GB")
        print(f"  mtime: {local_mtime}")
    else:
        print(f"Local: {QUANT_LOCAL} (not exists)")

    print(f"\nUpstream: {HF_REPO}/{HF_FILENAME}")
    print(f"  last_modified: {upstream_mtime}")

    # Disk space check
    disk_used_gb = shutil.disk_usage("/mnt/data").used / 1024**3
    disk_total_gb = shutil.disk_usage("/mnt/data").total / 1024**3
    disk_free_gb = shutil.disk_usage("/mnt/data").free / 1024**3
    pct = 100 * disk_used_gb / disk_total_gb
    print(f"\nDisk /mnt/data: {disk_used_gb:.1f}/{disk_total_gb:.1f} GB ({pct:.1f}% used, {disk_free_gb:.1f} GB free)")

    if disk_free_gb < 50:
        print("WARNING: меньше 50GB free. Download 36GB+ может упасть.")
        if not args.confirm:
            print("Запусти с --confirm чтобы всё равно попробовать.")

    if not args.confirm:
        print("\n=== DRY RUN === (используй --confirm для реального download)")
        return 0

    if local.exists() and not args.no_backup:
        print(f"\nBackup: {QUANT_LOCAL} → {BACKUP_PATH}")
        if Path(BACKUP_PATH).exists():
            os.remove(BACKUP_PATH)
        shutil.move(QUANT_LOCAL, BACKUP_PATH)

    print(f"\nDownloading {HF_REPO}/{HF_FILENAME}...")
    t0 = time.time()
    try:
        path = hf_hub_download(
            repo_id=HF_REPO,
            filename=HF_FILENAME,
            repo_type="dataset",
            local_dir=str(Path(QUANT_LOCAL).parent),
            force_download=True,
        )
    except Exception as exc:
        print(f"\nDOWNLOAD FAILED: {exc}")
        if Path(BACKUP_PATH).exists():
            print(f"Restoring from backup: {BACKUP_PATH} → {QUANT_LOCAL}")
            shutil.move(BACKUP_PATH, QUANT_LOCAL)
        return 1

    elapsed_min = (time.time() - t0) / 60
    new_size_gb = Path(path).stat().st_size / 1024**3
    print(f"\nDone in {elapsed_min:.1f} min, {new_size_gb:.1f} GB → {path}")

    if not args.no_backup:
        print(f"\nKeeping backup: {BACKUP_PATH} (delete manually after verifying new file)")

    print("\nNext steps:")
    print("  1. Inspect schema: python3 -c \"import duckdb; print(duckdb.sql('DESCRIBE SELECT * FROM read_parquet(\\\"/mnt/data/hf_data/quant.parquet\\\") LIMIT 1').fetchdf())\"")
    print("  2. Run ETL rebuild")
    print("  3. cron_runner.py ml-calibrator-fit")
    print("  4. cron_runner.py ml-meta-labeler-fit")
    return 0


if __name__ == "__main__":
    sys.exit(main())
