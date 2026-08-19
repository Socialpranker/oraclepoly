#!/usr/bin/env python3
"""Guard: джоба очереди 1 не шлёт в Telegram напрямую.

Acceptance §9 спеки 2026-08-04-notification-envelope-design: «Ни одна джоба
очереди 1 не вызывает send_telegram_message/send_admin_message напрямую — только
через шину (проверяется grep'ом в CI)».

Зачем guard вообще. Смысл единого конверта не в том, чтобы переписать 30 текстов,
а в том, чтобы 31-я джоба не смогла написать по-старому. Без автоматической
проверки правило держится на памяти автора — то есть не держится.

Белый список = очередь 2 (деньги и исполнение) + два осознанных исключения.
Он СОКРАЩАЕТСЯ по мере миграции: каждая мигрированная джоба убирается отсюда,
и обратно её уже не пустят.

Ищем вызовы `send_x(`, а не любое вхождение имени: в мигрированных джобах
остались объяснения в комментариях, почему старый путь был плох, и запрещать
упоминание значит запрещать документировать.
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

# Сканируем ОБА каталога. Раньше был только app/jobs/, и app/tasks/withdrawals.py —
# код, который двигает реальные деньги, — не был покрыт guard'ом вообще.
SCAN_DIRS = (Path("app/jobs"), Path("app/tasks"))

# Транспорт уведомлений. Сам обязан звать send_telegram_message — он и есть
# «напрямую», через который ходят все остальные.
TRANSPORT = {"app/tasks/notifications.py"}

# Очередь 2 — деньги и исполнение, мигрируется отдельным этапом (спека §7).
# Пути, а не голые имена: файлы в jobs/ и tasks/ могут называться одинаково,
# и разрешение одного не должно молча разрешать другой.
QUEUE_2 = {
    "app/jobs/withdrawal_propose.py",
    "app/tasks/withdrawals.py",
}
# Мигрированы из очереди 2 и обратно уже не пустят:
# пачка D — market_resolution_poller.py, stuck_live_positions_monitor.py;
# пачка E — execution_reconcile.py, redeem_job.py;
# пачка F — v2_execution_resolve.py, position_manager.py
#           (v2_execution.py см. BY_DESIGN — там осталась одна точка by design).

# Не входит ни в одну очередь — мониторы и отчёты вне скоупа спеки §7.
OUT_OF_SCOPE = {
    "app/jobs/agent_owners_report.py",
    "app/jobs/daily_summary.py",
    "app/jobs/polymarket_api_health.py",
    "app/jobs/ramp_check.py",
    "app/jobs/site_availability_monitor.py",
    "app/jobs/virtual_weekly_digest.py",
    "app/jobs/policy_engine_daily.py",  # свой канал advisory, мигрирован до конверта
}

# Осознанные исключения — обязаны слать напрямую by design.
BY_DESIGN = {
    # Daily Brief не источник, а КОНЕЧНАЯ ТОЧКА FYI-потока: он забирает
    # накопленное из notification_fyi_queue. Пропустить его собственный текст
    # через шину = рекурсия (публикует себя → строка в очередь → следующий
    # бриф забирает и снова публикует).
    "app/jobs/agent_daily_brief.py",
    # v2_execution: все алерты мигрированы, но _notify_live_bet остаётся прямым
    # по §7 спеки. Это не текст-в-очередь, а операционный UI: карточка реальной
    # ставки с кнопкой «Закрыть позицию» (callback_data → agent_actions).
    # Через дедуп шины она ходить не должна — каждая живая ставка своя, и
    # подавить её нельзя даже теоретически.
    "app/jobs/v2_execution.py",
}

ALLOWED = QUEUE_2 | OUT_OF_SCOPE | BY_DESIGN | TRANSPORT

# Именно ВЫЗОВ, не упоминание в комментарии/докстринге.
CALL_RE = re.compile(r"\bawait\s+send_(telegram_message|admin_message)\s*\(")


def main() -> int:
    offenders: list[tuple[str, int, str]] = []
    for scan_dir in SCAN_DIRS:
        if not scan_dir.is_dir():
            continue
        for path in sorted(scan_dir.glob("*.py")):
            # Сверяем ПУТЬ, не имя файла: одноимённые модули в jobs/ и tasks/
            # не должны разрешать друг друга.
            if path.as_posix() in ALLOWED:
                continue
            for lineno, line in enumerate(path.read_text().splitlines(), 1):
                if CALL_RE.search(line):
                    offenders.append((str(path), lineno, line.strip()))

    if not offenders:
        return 0

    print("❌ Джоба шлёт в Telegram напрямую, минуя шину уведомлений (Acceptance §9):")
    for file, lineno, line in offenders:
        print(f"   {file}:{lineno}: {line}")
    print()
    print("   Отдай структуру шине: to_notifications(...) + await publish(..., actor='<имя джобы>').")
    print("   Образец — app/jobs/db_size_monitor.py.")
    print("   Если джоба обязана слать напрямую by design — добавь её в BY_DESIGN")
    print(f"   в {__file__} С ОБЪЯСНЕНИЕМ ПОЧЕМУ.")
    return 1


if __name__ == "__main__":
    sys.exit(main())
