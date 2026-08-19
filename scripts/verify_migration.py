#!/usr/bin/env python3
"""
OraclePoly — Верификация PostgreSQL

Использование:
    python scripts/verify_migration.py

Переменная окружения:
    DATABASE_URL — connection string к локальному PostgreSQL
    По умолчанию: postgresql://oraclepoly:password@localhost:5432/oraclepoly

Проверки:
    1. Подключение к БД
    2. Наличие таблиц
    3. Количество строк в ключевых таблицах
    4. RPC-функции (SQL functions)
    5. Расширения (uuid-ossp, pg_trgm, pgcrypto, pg_stat_statements)
    6. Триггеры realtime
    7. Smoke-тест: вставка + чтение + удаление
"""

import asyncio
import os
import sys
import uuid

import asyncpg

DATABASE_URL = os.getenv(
    "DATABASE_URL",
    "postgresql://oraclepoly:password@localhost:5432/oraclepoly",
)

# Минимально ожидаемые таблицы
EXPECTED_TABLES = [
    "markets",
    "transactions",
    "whales",
    "news",
    "news_market_mappings",
    "black_swan_scenarios",
    "oracle_snapshots",
    "wallet_mappings",
    "users",
    "notifications",
    "events",
    "votes",
    "achievements",
    "user_achievements",
    "subscriptions",
]

# RPC-функции, которые должны существовать
EXPECTED_FUNCTIONS = [
    "cast_vote_atomic",
    "get_leaderboard",
    "check_and_grant_achievements",
    "create_prediction_with_snapshot",
    "grant_credits",
    "refresh_top_analysts",
]

# Расширения
EXPECTED_EXTENSIONS = [
    "uuid-ossp",
    "pg_trgm",
    "pgcrypto",
    "pg_stat_statements",
]


class Colors:
    OK = "\033[92m"
    FAIL = "\033[91m"
    WARN = "\033[93m"
    BOLD = "\033[1m"
    END = "\033[0m"


def ok(msg: str) -> None:
    print(f"  {Colors.OK}✓{Colors.END} {msg}")


def fail(msg: str) -> None:
    print(f"  {Colors.FAIL}✗{Colors.END} {msg}")


def warn(msg: str) -> None:
    print(f"  {Colors.WARN}⚠{Colors.END} {msg}")


async def check_connection(pool: asyncpg.Pool) -> bool:
    print(f"\n{Colors.BOLD}1. Подключение к БД{Colors.END}")
    try:
        version = await pool.fetchval("SELECT version()")
        ok(f"PostgreSQL: {version[:60]}...")
        return True
    except Exception as e:
        fail(f"Не удалось подключиться: {e}")
        return False


async def check_extensions(pool: asyncpg.Pool) -> int:
    print(f"\n{Colors.BOLD}2. Расширения{Colors.END}")
    errors = 0
    installed = await pool.fetch(
        "SELECT extname FROM pg_extension WHERE extname = ANY($1)",
        EXPECTED_EXTENSIONS,
    )
    installed_names = {r["extname"] for r in installed}

    for ext in EXPECTED_EXTENSIONS:
        if ext in installed_names:
            ok(ext)
        else:
            fail(f"{ext} — не установлено")
            errors += 1
    return errors


async def check_tables(pool: asyncpg.Pool) -> int:
    print(f"\n{Colors.BOLD}3. Таблицы{Colors.END}")
    errors = 0
    rows = await pool.fetch(
        "SELECT tablename FROM pg_tables WHERE schemaname = 'public' ORDER BY tablename"
    )
    existing = {r["tablename"] for r in rows}

    for table in EXPECTED_TABLES:
        if table in existing:
            ok(table)
        else:
            fail(f"{table} — не найдена")
            errors += 1

    extra = existing - set(EXPECTED_TABLES)
    if extra:
        print(f"\n  Дополнительные таблицы: {', '.join(sorted(extra))}")

    return errors


async def check_row_counts(pool: asyncpg.Pool) -> int:
    print(f"\n{Colors.BOLD}4. Количество строк{Colors.END}")
    warnings = 0

    for table in EXPECTED_TABLES:
        try:
            count = await pool.fetchval(f'SELECT count(*) FROM "{table}"')
            if count == 0:
                warn(f"{table}: 0 строк (пусто)")
                warnings += 1
            else:
                ok(f"{table}: {count:,} строк")
        except asyncpg.UndefinedTableError:
            fail(f"{table}: таблица не существует")
            warnings += 1
        except Exception as e:
            fail(f"{table}: ошибка — {e}")
            warnings += 1

    return warnings


async def check_functions(pool: asyncpg.Pool) -> int:
    print(f"\n{Colors.BOLD}5. RPC-функции{Colors.END}")
    errors = 0

    rows = await pool.fetch(
        """
        SELECT p.proname
        FROM pg_proc p
        JOIN pg_namespace n ON p.pronamespace = n.oid
        WHERE n.nspname = 'public'
          AND p.proname = ANY($1)
        """,
        EXPECTED_FUNCTIONS,
    )
    existing = {r["proname"] for r in rows}

    for func in EXPECTED_FUNCTIONS:
        if func in existing:
            ok(func)
        else:
            fail(f"{func} — не найдена")
            errors += 1

    return errors


async def check_triggers(pool: asyncpg.Pool) -> int:
    print(f"\n{Colors.BOLD}6. Триггеры realtime{Colors.END}")
    errors = 0

    expected_triggers = {
        "markets": "notify_markets_change",
        "transactions": "notify_transactions_change",
        "black_swan_scenarios": "notify_scenarios_change",
    }

    rows = await pool.fetch(
        """
        SELECT trigger_name, event_object_table
        FROM information_schema.triggers
        WHERE trigger_schema = 'public'
          AND event_object_table = ANY($1)
        """,
        list(expected_triggers.keys()),
    )
    found = {r["event_object_table"]: r["trigger_name"] for r in rows}

    for table, trigger in expected_triggers.items():
        if table in found:
            ok(f"{table} → {found[table]}")
        else:
            fail(f"{table} → триггер не найден")
            errors += 1

    return errors


async def smoke_test(pool: asyncpg.Pool) -> int:
    print(f"\n{Colors.BOLD}7. Smoke-тест (INSERT → SELECT → DELETE){Colors.END}")
    errors = 0
    test_id = str(uuid.uuid4())

    try:
        # INSERT
        await pool.execute(
            """
            INSERT INTO notifications (id, user_id, type, title, body)
            VALUES ($1, '00000000-0000-0000-0000-000000000000', 'test', 'Migration Test', 'Auto-test')
            """,
            uuid.UUID(test_id),
        )
        ok("INSERT — ок")

        # SELECT
        row = await pool.fetchrow(
            "SELECT id, title FROM notifications WHERE id = $1",
            uuid.UUID(test_id),
        )
        if row and row["title"] == "Migration Test":
            ok("SELECT — ок")
        else:
            fail("SELECT — данные не совпадают")
            errors += 1

        # DELETE
        await pool.execute(
            "DELETE FROM notifications WHERE id = $1",
            uuid.UUID(test_id),
        )
        ok("DELETE — ок")

    except Exception as e:
        fail(f"Smoke-тест: {e}")
        errors += 1

    return errors


async def main() -> int:
    print(f"{Colors.BOLD}{'=' * 50}")
    print("OraclePoly — Верификация миграции")
    print(f"{'=' * 50}{Colors.END}")
    print(f"DATABASE_URL: {DATABASE_URL[:50]}...")

    try:
        pool = await asyncpg.create_pool(dsn=DATABASE_URL, min_size=1, max_size=3, command_timeout=10)
    except Exception as e:
        fail(f"Не удалось подключиться к БД: {e}")
        return 1

    total_errors = 0
    total_warnings = 0

    try:
        if not await check_connection(pool):
            return 1

        total_errors += await check_extensions(pool)
        total_errors += await check_tables(pool)
        total_warnings += await check_row_counts(pool)
        total_errors += await check_functions(pool)
        total_errors += await check_triggers(pool)
        total_errors += await smoke_test(pool)

    finally:
        await pool.close()

    # Итог
    print(f"\n{Colors.BOLD}{'=' * 50}")
    print(f"Итог{Colors.END}")
    if total_errors == 0:
        print(f"{Colors.OK}  Ошибки: 0{Colors.END}")
    else:
        print(f"{Colors.FAIL}  Ошибки: {total_errors}{Colors.END}")

    if total_warnings > 0:
        print(f"{Colors.WARN}  Предупреждения: {total_warnings} (пустые таблицы){Colors.END}")

    if total_errors == 0:
        print(f"\n{Colors.OK}{Colors.BOLD}Миграция верифицирована успешно!{Colors.END}")
    else:
        print(f"\n{Colors.FAIL}{Colors.BOLD}Есть ошибки — проверьте вывод выше{Colors.END}")

    return 1 if total_errors > 0 else 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
