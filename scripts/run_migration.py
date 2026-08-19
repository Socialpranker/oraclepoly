#!/usr/bin/env python3
"""
OraclePoly — Smart Migration Runner with State Tracking.

Usage:
    # Apply all pending migrations:
    python scripts/run_migration.py

    # Apply a single migration file:
    python scripts/run_migration.py migrations/188_new_feature.sql

    # Show migration status (applied / pending):
    python scripts/run_migration.py --status

How it works:
    1. Ensures `schema_migrations` table exists in the database.
    2. Reads all *.sql files from the `migrations/` directory, sorted by name.
    3. Compares with already-applied migrations stored in `schema_migrations`.
    4. Applies each pending migration inside a transaction.
    5. Records the filename in `schema_migrations` on success.
    6. Stops immediately on first error (transaction is rolled back).
"""

import asyncio
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from dotenv import load_dotenv

load_dotenv()

# ─── Constants ───────────────────────────────────────────────
MIGRATIONS_DIR = Path(__file__).parent.parent / "migrations"

SCHEMA_MIGRATIONS_DDL = """
CREATE TABLE IF NOT EXISTS schema_migrations (
    id          SERIAL PRIMARY KEY,
    filename    TEXT UNIQUE NOT NULL,
    checksum    TEXT,
    applied_at  TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
"""


class Colors:
    OK = "\033[92m"
    FAIL = "\033[91m"
    WARN = "\033[93m"
    BOLD = "\033[1m"
    DIM = "\033[2m"
    END = "\033[0m"


def ok(msg: str) -> None:
    print(f"  {Colors.OK}✓{Colors.END} {msg}")


def fail(msg: str) -> None:
    print(f"  {Colors.FAIL}✗{Colors.END} {msg}")


def warn(msg: str) -> None:
    print(f"  {Colors.WARN}⚠{Colors.END} {msg}")


def info(msg: str) -> None:
    print(f"  {Colors.DIM}→{Colors.END} {msg}")


def _get_db_url() -> str:
    """Get database URL from Pydantic Settings (same as app) or env fallback."""
    # Try Pydantic Settings first (reads .env + env vars)
    try:
        from app.core.config import get_settings
        return get_settings().database_url
    except Exception:
        pass
    # Fallback to raw env var
    db_url = os.getenv("DATABASE_URL")
    if not db_url:
        fail("DATABASE_URL not set. Set it in .env or as environment variable.")
        sys.exit(1)
    return db_url


def _get_pending_files() -> list[Path]:
    """Return sorted list of .sql migration files."""
    if not MIGRATIONS_DIR.exists():
        fail(f"Migrations directory not found: {MIGRATIONS_DIR}")
        sys.exit(1)
    files = sorted(MIGRATIONS_DIR.glob("*.sql"))
    return files


def _checksum(content: str) -> str:
    """Simple MD5 checksum for migration content."""
    import hashlib

    return hashlib.md5(content.encode()).hexdigest()


# ─── Core Logic ──────────────────────────────────────────────


async def _ensure_schema_table(conn) -> None:
    """Create schema_migrations table if it doesn't exist."""
    await conn.execute(SCHEMA_MIGRATIONS_DDL)


async def _get_applied(conn) -> set[str]:
    """Return set of already-applied migration filenames."""
    rows = await conn.fetch("SELECT filename FROM schema_migrations ORDER BY id")
    return {r["filename"] for r in rows}


async def apply_all() -> int:
    """Apply all pending migrations. Returns number of applied migrations."""
    import asyncpg

    db_url = _get_db_url()
    conn = await asyncpg.connect(db_url)

    try:
        await _ensure_schema_table(conn)
        applied = await _get_applied(conn)
        all_files = _get_pending_files()

        pending = [f for f in all_files if f.name not in applied]

        if not pending:
            ok("All migrations are up to date. Nothing to apply.")
            return 0

        print(
            f"\n{Colors.BOLD}Applying {len(pending)} pending migration(s)...{Colors.END}\n"
        )

        count = 0
        for path in pending:
            sql = path.read_text()
            cs = _checksum(sql)

            try:
                async with conn.transaction():
                    await conn.execute(sql)
                    await conn.execute(
                        "INSERT INTO schema_migrations (filename, checksum) VALUES ($1, $2)",
                        path.name,
                        cs,
                    )
                ok(f"{path.name}")
                count += 1
            except Exception as e:
                fail(f"{path.name} — FAILED: {e}")
                print(
                    f"\n{Colors.FAIL}Stopping. Transaction rolled back for {path.name}.{Colors.END}"
                )
                return count

        print(
            f"\n{Colors.OK}{Colors.BOLD}Done! Applied {count} migration(s).{Colors.END}"
        )
        return count

    finally:
        await conn.close()


async def apply_single(sql_file: str) -> None:
    """Apply a single migration file (legacy mode, with state tracking)."""
    import asyncpg

    path = Path(sql_file)
    if not path.exists():
        fail(f"File not found: {sql_file}")
        sys.exit(1)

    db_url = _get_db_url()
    conn = await asyncpg.connect(db_url)

    try:
        await _ensure_schema_table(conn)
        applied = await _get_applied(conn)

        if path.name in applied:
            warn(f"{path.name} already applied. Skipping.")
            return

        sql = path.read_text()
        cs = _checksum(sql)

        print(f"Running migration: {path.name}")
        print("=" * 50)
        print(sql[:500] + "..." if len(sql) > 500 else sql)
        print("=" * 50)

        try:
            async with conn.transaction():
                await conn.execute(sql)
                await conn.execute(
                    "INSERT INTO schema_migrations (filename, checksum) VALUES ($1, $2)",
                    path.name,
                    cs,
                )
            ok(f"Migration {path.name} applied successfully.")
        except Exception as e:
            fail(f"ERROR applying migration: {e}")
            sys.exit(1)

    finally:
        await conn.close()


async def show_status() -> None:
    """Show which migrations are applied and which are pending."""
    import asyncpg

    db_url = _get_db_url()
    conn = await asyncpg.connect(db_url)

    try:
        await _ensure_schema_table(conn)
        applied = await _get_applied(conn)
        all_files = _get_pending_files()

        print(f"\n{Colors.BOLD}Migration Status{Colors.END}")
        print(f"{'=' * 60}")
        print(f"  Total files:   {len(all_files)}")
        print(f"  Applied:       {len(applied)}")
        print(f"  Pending:       {len(all_files) - len([f for f in all_files if f.name in applied])}")
        print(f"{'=' * 60}\n")

        for path in all_files:
            if path.name in applied:
                ok(f"{path.name}")
            else:
                warn(f"{path.name} {Colors.WARN}(pending){Colors.END}")

    finally:
        await conn.close()


# ─── CLI ─────────────────────────────────────────────────────

if __name__ == "__main__":
    if len(sys.argv) < 2:
        # No args → apply all pending
        asyncio.run(apply_all())
    elif sys.argv[1] == "--status":
        asyncio.run(show_status())
    elif sys.argv[1].endswith(".sql"):
        asyncio.run(apply_single(sys.argv[1]))
    else:
        print(__doc__)
        sys.exit(1)
