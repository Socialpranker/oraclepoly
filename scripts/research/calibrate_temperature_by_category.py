"""
Калибровка Temperature по категориям на основе bankroll_sim_resolved_trades.csv.

Запуск:
    python scripts/research/calibrate_temperature_by_category.py [--dry-run] [--csv PATH]

Алгоритм:
    1. Загружает resolved trades из CSV (или из БД через CalibrationRepository)
    2. Для каждой категории grid search оптимальной T (минимизирует Log Loss)
    3. Сравнивает с текущей T=1.15 (hardcoded)
    4. Вставляет новые calibration_configs в БД и активирует их
    5. Инвалидирует Redis кеш

Использует только temperature — остальные веса (w_whale, w_micro, w_registry)
остаются из текущего активного конфига (не перезатираются).
"""

import argparse
import asyncio
import csv
import math
import sys
import uuid
from pathlib import Path

# Добавляем корень проекта в path для импортов app.*
sys.path.insert(0, str(Path(__file__).parent.parent.parent))

# --- Pure math (без импортов app) ---

_EPSILON = 1e-7


def sigmoid(x: float) -> float:
    x = max(-500.0, min(500.0, x))
    return 1.0 / (1.0 + math.exp(-x))


def log_loss(preds: list[float], outcomes: list[int]) -> float:
    n = len(preds)
    if n == 0:
        return 0.0
    total = sum(
        y * math.log(max(_EPSILON, p)) + (1 - y) * math.log(max(_EPSILON, 1 - p))
        for p, y in zip(preds, outcomes)
    )
    return -total / n


def brier_score(preds: list[float], outcomes: list[int]) -> float:
    n = len(preds)
    if n == 0:
        return 0.0
    return sum((p - y) ** 2 for p, y in zip(preds, outcomes)) / n


def find_optimal_temperature(
    z_vals: list[float],
    outcomes: list[int],
    t_min: float = 0.2,
    t_max: float = 5.0,
    steps: int = 200,
) -> tuple[float, float]:
    """Grid search T* минимизирующего Log Loss. Возвращает (T*, best_log_loss)."""
    best_t, best_ll = 1.0, float("inf")
    for i in range(steps + 1):
        t = t_min + (t_max - t_min) * i / steps
        preds = [sigmoid(z / t) for z in z_vals]
        ll = log_loss(preds, outcomes)
        if ll < best_ll:
            best_ll = ll
            best_t = t
    return best_t, best_ll


def load_csv(path: str) -> list[dict]:
    with open(path, newline="") as f:
        return list(csv.DictReader(f))


def prepare_category_data(rows: list[dict]) -> dict[str, tuple[list[float], list[int]]]:
    """
    Возвращает dict: category → (z_vals, outcomes).

    z_posterior флипается для side=NO (чтобы z > 0 = предсказываем win).
    """
    by_cat: dict[str, tuple[list, list]] = {}
    for r in rows:
        z_str = r.get("z_posterior", "")
        if not z_str:
            continue
        try:
            z = float(z_str)
        except ValueError:
            continue
        if r["side"] == "NO":
            z = -z
        cat = r.get("category") or "global"
        win = int(r["win"])
        if cat not in by_cat:
            by_cat[cat] = ([], [])
        by_cat[cat][0].append(z)
        by_cat[cat][1].append(win)
    return by_cat


def compute_results(rows: list[dict], current_t: float = 1.15) -> list[dict]:
    """
    Для каждой категории (и global) находит оптимальную T.
    Возвращает список результатов с метриками.
    """
    by_cat = prepare_category_data(rows)

    # Добавляем global (все строки вместе)
    all_z: list[float] = []
    all_y: list[int] = []
    for z_list, y_list in by_cat.values():
        all_z.extend(z_list)
        all_y.extend(y_list)
    by_cat["global"] = (all_z, all_y)

    MIN_SAMPLES = 500  # меньше — не калибруем, используем global T

    results = []
    for cat, (z_vals, outcomes) in by_cat.items():
        n = len(z_vals)
        if n < MIN_SAMPLES and cat != "global":
            print(f"  {cat:20s}: N={n} < {MIN_SAMPLES} — пропускаем (будет global fallback)")
            continue

        # Текущие метрики при T=current_t
        preds_curr = [sigmoid(z / current_t) for z in z_vals]
        ll_curr = log_loss(preds_curr, outcomes)
        bs_curr = brier_score(preds_curr, outcomes)

        # Оптимальная T
        opt_t, ll_opt = find_optimal_temperature(z_vals, outcomes)
        preds_opt = [sigmoid(z / opt_t) for z in z_vals]
        bs_opt = brier_score(preds_opt, outcomes)

        wr = sum(outcomes) / n
        naive_bs = wr * (1 - wr)
        skill_curr = 1 - bs_curr / naive_bs if naive_bs > 0 else 0
        skill_opt = 1 - bs_opt / naive_bs if naive_bs > 0 else 0

        delta_ll = ll_curr - ll_opt  # положительное = улучшение
        delta_pct = delta_ll / ll_curr * 100 if ll_curr > 0 else 0

        results.append(
            {
                "category": cat,
                "n": n,
                "current_t": current_t,
                "optimal_t": opt_t,
                "ll_current": ll_curr,
                "ll_optimal": ll_opt,
                "ll_delta_pct": delta_pct,
                "bs_current": bs_curr,
                "bs_optimal": bs_opt,
                "skill_current": skill_curr,
                "skill_optimal": skill_opt,
            }
        )

    return results


def print_results(results: list[dict]) -> None:
    print()
    print(f"{'Категория':20s} | {'N':>6} | {'T curr':>7} | {'T opt':>6} | {'LL curr':>8} | {'LL opt':>8} | {'ΔLL%':>6} | {'Skill curr':>10} | {'Skill opt':>9}")
    print("-" * 105)
    for r in sorted(results, key=lambda x: (x["category"] != "global", x["category"])):
        print(
            f"{r['category']:20s} | {r['n']:6d} | {r['current_t']:7.3f} | {r['optimal_t']:6.3f} | "
            f"{r['ll_current']:8.4f} | {r['ll_optimal']:8.4f} | {r['ll_delta_pct']:+6.2f}% | "
            f"{r['skill_current']:+10.3f} | {r['skill_optimal']:+9.3f}"
        )
    print()


async def write_to_db(results: list[dict], dry_run: bool) -> None:
    """Записывает новые calibration_configs в БД и активирует их."""
    if dry_run:
        for r in results:
            opt_t = round(r["optimal_t"], 4)
            print(f"  [dry-run] {r['category']:20s}: T {r['current_t']:.3f} → {opt_t:.4f} | ΔLL {r['ll_delta_pct']:+.2f}%")
        return

    from app.core.cache import close_redis, init_redis
    from app.core.config import get_settings
    from app.services.calibration_service import invalidate_calibration_cache
    from app.services.database import close_db, execute, fetchrow, init_db, record_to_dict

    settings = get_settings()
    await init_db()
    await init_redis(settings.redis_url)

    try:
        for r in results:
            cat = r["category"]
            opt_t = round(r["optimal_t"], 4)

            # Загружаем текущий активный конфиг чтобы унаследовать веса
            current_row = await fetchrow(
                "SELECT * FROM calibration_configs WHERE category = $1 AND is_active = true LIMIT 1",
                cat,
            )
            current = record_to_dict(current_row) if current_row else {}

            # Новый конфиг: всё из текущего, только temperature меняем
            new_config = {
                "id": str(uuid.uuid4()),
                "category": cat,
                "w_mkt": float(current.get("w_mkt") or 1.0),
                "w_whale": float(current.get("w_whale") or 0.5),
                "w_news": float(current.get("w_news") or 0.0),
                "w_fresh": float(current.get("w_fresh") or 0.0),
                "w_cluster": float(current.get("w_cluster") or 0.0),
                "w_registry": float(current.get("w_registry") or 0.3),
                "w_oi": float(current.get("w_oi") or 0.0),
                "w_concentration": float(current.get("w_concentration") or 0.0),
                "w_micro": float(current.get("w_micro") or 0.35),
                "temperature": opt_t,
                "beta_a": float(current.get("beta_a") or 1.0),
                "beta_b": float(current.get("beta_b") or 0.0),
                "calibration_method": current.get("calibration_method") or "temperature",
                "is_active": False,
                "source": "csv_calibration_2026_04_05",
                "brier_score_before": round(r["bs_current"], 6),
                "brier_score_after": round(r["bs_optimal"], 6),
                "log_loss_before": round(r["ll_current"], 6),
                "log_loss_after": round(r["ll_optimal"], 6),
                "sample_count": r["n"],
            }

            # INSERT нового конфига
            await execute(
                """
                INSERT INTO calibration_configs
                    (id, category, w_mkt, w_whale, w_news, w_fresh, w_cluster, w_registry,
                     w_oi, w_concentration, w_micro, temperature, beta_a, beta_b,
                     calibration_method, is_active, source,
                     brier_score_before, brier_score_after, log_loss_before, log_loss_after,
                     sample_count)
                VALUES
                    ($1, $2, $3, $4, $5, $6, $7, $8,
                     $9, $10, $11, $12, $13, $14,
                     $15, $16, $17,
                     $18, $19, $20, $21,
                     $22)
                """,
                new_config["id"], new_config["category"],
                new_config["w_mkt"], new_config["w_whale"], new_config["w_news"],
                new_config["w_fresh"], new_config["w_cluster"], new_config["w_registry"],
                new_config["w_oi"], new_config["w_concentration"], new_config["w_micro"],
                new_config["temperature"], new_config["beta_a"], new_config["beta_b"],
                new_config["calibration_method"], new_config["is_active"], new_config["source"],
                new_config["brier_score_before"], new_config["brier_score_after"],
                new_config["log_loss_before"], new_config["log_loss_after"],
                new_config["sample_count"],
            )

            # Деактивировать старый, активировать новый
            await execute(
                "UPDATE calibration_configs SET is_active = false WHERE category = $1 AND is_active = true",
                cat,
            )
            await execute(
                "UPDATE calibration_configs SET is_active = true WHERE id = $1",
                new_config["id"],
            )
            print(f"  ✓ {cat}: T {r['current_t']:.3f} → {opt_t:.4f} (N={r['n']}, ΔLL {r['ll_delta_pct']:+.2f}%)")

        if not dry_run:
            count = await invalidate_calibration_cache()
            print(f"\n  Redis cache инвалидирован ({count} ключей)")

    finally:
        await close_db()
        await close_redis()


async def main() -> None:
    parser = argparse.ArgumentParser(description="Калибровка Temperature по категориям")
    parser.add_argument("--dry-run", action="store_true", help="Не писать в БД, только показать результаты")
    parser.add_argument("--csv", default="research/bankroll_sim_resolved_trades.csv", help="Путь к CSV файлу")
    parser.add_argument("--current-t", type=float, default=1.15, help="Текущая температура для сравнения")
    args = parser.parse_args()

    csv_path = Path(args.csv)
    if not csv_path.exists():
        print(f"Файл не найден: {csv_path}")
        sys.exit(1)

    print(f"Загружаем данные из {csv_path}...")
    rows = load_csv(str(csv_path))
    print(f"Загружено {len(rows):,} строк")

    print("\nВычисляем оптимальные температуры...")
    results = compute_results(rows, current_t=args.current_t)

    print_results(results)

    # Фильтруем: применяем только если улучшение > 0.1% LL и N >= 50
    significant = [r for r in results if r["ll_delta_pct"] > 0.1]
    skipped = [r for r in results if r["ll_delta_pct"] <= 0.1]

    if skipped:
        print(f"Пропускаем (улучшение < 0.1%): {[r['category'] for r in skipped]}")

    if not significant:
        print("Нет значимых улучшений — запись в БД не нужна.")
        return

    print(f"Будет обновлено: {[r['category'] for r in significant]}")

    if args.dry_run:
        print("\n[DRY RUN] — в БД не пишем:\n")
        await write_to_db(significant, dry_run=True)
    else:
        print("\nЗаписываем в БД...")
        await write_to_db(significant, dry_run=False)
        print("\nГотово. Новые температуры активны.")


if __name__ == "__main__":
    asyncio.run(main())
