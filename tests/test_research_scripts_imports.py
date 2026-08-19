"""
Регрессионный тест: research-скрипты должны корректно настраивать sys.path
для импорта `app.*` после reorganization 2026-04-29 (PR #304 → scripts/research/).

История: PR #304 переместил 28 скриптов в scripts/research/, но 12 из них
использовали `os.path.dirname(os.path.dirname(__file__))` (2 уровня вверх) или
`Path(__file__).parent.parent` — что давало `scripts/`, не корень проекта.
ImportError: No module named 'app' при первом запуске Phase 1 smoke test.

Этот тест парсит каждый research-скрипт через AST и проверяет, что:
1. Если есть sys.path.insert — он указывает на правильный уровень (3 dirname или parent.parent.parent)
2. Если используется ROOT константа — она тоже на 3 уровня вверх
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

RESEARCH_DIR = Path(__file__).parent.parent / "scripts" / "research"


def _read(path: Path) -> str:
    return path.read_text()


def _has_app_import(src: str) -> bool:
    """True если файл импортирует `app.*` или `from app.*`."""
    tree = ast.parse(src)
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module and node.module.startswith("app"):
            return True
        if isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name.startswith("app"):
                    return True
    return False


@pytest.fixture(scope="module")
def research_files() -> list[Path]:
    files = sorted(RESEARCH_DIR.glob("*.py"))
    assert len(files) > 0, "scripts/research/ должен содержать .py файлы"
    return files


def test_research_dir_exists():
    assert RESEARCH_DIR.is_dir(), f"{RESEARCH_DIR} not found"


def test_no_two_level_dirname_pattern(research_files: list[Path]):
    """Регрессия: после переезда в scripts/research/ нельзя использовать
    `os.path.dirname(os.path.dirname(os.path.abspath(__file__)))` (2x) — это даёт
    scripts/, не корень. Применяется к sys.path.insert И к os.path.join (для default paths).

    Идём через AST: считаем число вложенных `os.path.dirname(...)` в любом expression
    содержащем `os.path.abspath(__file__)`. Должно быть 3.
    """
    import ast

    def _count_dirname_chain(node: ast.AST) -> int | None:
        """Если node — цепочка os.path.dirname(...) с os.path.abspath(__file__) внутри,
        вернуть длину цепочки. Иначе None."""
        # Должен быть Call to os.path.dirname
        depth = 0
        current = node
        while (
            isinstance(current, ast.Call)
            and isinstance(current.func, ast.Attribute)
            and current.func.attr == "dirname"
            and isinstance(current.func.value, ast.Attribute)
            and current.func.value.attr == "path"
        ):
            depth += 1
            if not current.args:
                return None
            current = current.args[0]
        # Внутри должен быть os.path.abspath(__file__)
        if (
            isinstance(current, ast.Call)
            and isinstance(current.func, ast.Attribute)
            and current.func.attr == "abspath"
            and current.args
            and isinstance(current.args[0], ast.Name)
            and current.args[0].id == "__file__"
        ):
            return depth
        return None

    def _is_dirname_call(node: ast.AST) -> bool:
        return (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr == "dirname"
            and isinstance(node.func.value, ast.Attribute)
            and node.func.value.attr == "path"
        )

    bad_files = []
    for path in research_files:
        src = _read(path)
        try:
            tree = ast.parse(src)
        except SyntaxError:
            continue
        for node in ast.walk(tree):
            depth = _count_dirname_chain(node)
            if depth is None or depth >= 3:
                continue
            # Берём только outermost — если parent тоже dirname-call, skip
            # (ast.walk не даёт parents, восстановим вручную через `parents` map)
            # Простая эвристика: если это inner вызов внутри dirname, skip
            # Это можно проверить: если result из _count_dirname_chain меньше чем у
            # потенциального outer, не считать его. На практике нам достаточно одного
            # bad report per file, поэтому уникализируем по файлу.
            line_no = getattr(node, "lineno", "?")
            entry = f"{path.name}:{line_no}"
            if not any(entry in b for b in bad_files):
                bad_files.append(f"{entry} (depth={depth})")
    # Финальный фильтр: если для одной строки есть depth=3 и depth=2 — это normal
    # (depth=3 outer, depth=2 inner). Уберём строки где есть только bad нo нет good.
    # Build per-file-line max depth:
    line_max_depth: dict[str, int] = {}
    for path in research_files:
        src = _read(path)
        try:
            tree = ast.parse(src)
        except SyntaxError:
            continue
        for node in ast.walk(tree):
            depth = _count_dirname_chain(node)
            if depth is None:
                continue
            line_no = getattr(node, "lineno", "?")
            key = f"{path.name}:{line_no}"
            line_max_depth[key] = max(line_max_depth.get(key, 0), depth)

    real_bad = [
        f"{key} (max depth={line_max_depth[key]})"
        for key in line_max_depth
        if line_max_depth[key] < 3
    ]
    assert not real_bad, (
        f"Скрипты с <3 dirname'ами на верхнем уровне: {real_bad}. "
        f"После переезда в scripts/research/ нужно 3 dirname для корня проекта."
    )


def test_no_two_level_parent_pattern(research_files: list[Path]):
    """Регрессия: `Path(__file__).parent.parent` даёт scripts/, не корень."""
    import re

    # Pattern: .parent.parent НЕ followed by .parent
    # И только если это применяется к Path(__file__) (исключим случайные совпадения)
    bad_re = re.compile(r"Path\(__file__\)(?:\.resolve\(\))?\.parent\.parent(?!\.parent)")
    bad_files = []
    for path in research_files:
        src = _read(path)
        for m in bad_re.finditer(src):
            line_no = src[: m.start()].count("\n") + 1
            bad_files.append(f"{path.name}:{line_no}")
    assert not bad_files, (
        f"Скрипты с `Path(__file__).parent.parent` (без третьего .parent): {bad_files}. "
        f"После переезда в scripts/research/ нужно .parent.parent.parent для корня проекта."
    )


@pytest.mark.parametrize("script_name", [
    "historical_pit_loader.py",
    "filter_audit.py",
    "global_filters_ablation.py",
    "backtest_historical.py",
    "strategy_backtest_oracle.py",
    "strategy_volume_backtest.py",
    "strategy_discovery_oracle_tournament.py",
    "calibrate_temperature_by_category.py",
    "backtest_cluster_strategies.py",
    "dry_pipeline_reset.py",
    "strategy_backtest_all.py",
    "backtest_stake_comparison.py",
    "strategy_discovery_prod.py",
])
def test_critical_scripts_import_app_correctly(script_name: str):
    """Точечный регресс-тест: критичные скрипты с `from app.X` должны иметь
    корректный sys.path или ROOT (3 уровня вверх от scripts/research/)."""
    path = RESEARCH_DIR / script_name
    if not path.exists():
        pytest.skip(f"{script_name} not in research/ (возможно перенесён или удалён)")

    src = _read(path)
    has_app = _has_app_import(src)
    if not has_app:
        pytest.skip(f"{script_name} не импортирует app.* — sys.path не нужен")

    # Должен быть один из правильных паттернов:
    correct_patterns = [
        "os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))",  # 3x dirname
        "Path(__file__).parent.parent.parent",  # 3x parent
        "Path(__file__).resolve().parent.parent.parent",  # 3x parent с resolve
    ]
    has_correct = any(p in src for p in correct_patterns)

    # Альтернативно — скрипт может полагаться на cwd (запуск из корня)
    # В этом случае sys.path.insert вообще отсутствует
    has_sys_path_insert = "sys.path.insert" in src
    has_root_const = "ROOT = " in src

    if has_sys_path_insert or has_root_const:
        assert has_correct, (
            f"{script_name} имеет sys.path/ROOT, но без правильного 3x parent/dirname. "
            f"Должно быть одно из: {correct_patterns}"
        )
