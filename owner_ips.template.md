# Owner IPS — шаблон (Phase 2 C2)
#
# Скопируй в owner_ips.local.md (gitignored) и заполни цифры.
# Формат СТРОГИЙ: строки `key: value`, комментарии `#`, пустые строки.
# Парсер (app/analytics/ips.py) отвергает неизвестные ключи, отсутствие
# обязательных, и непарсящиеся значения. Проза не допускается.
# Изменение политики = новая версия файла (hash меняется автоматически).
# Спека: docs/specs/portfolio_analyst_agent.md §9.1.
#
# capital_usd            — капитал операции, USD (число)
# max_drawdown_pct       — макс. просадка портфеля, % как отрицательная доля (напр. -0.20)
# liquidity_need_usd     — потребность в ликвидности, USD (число)
# weekly_time_budget_hours — бюджет времени в неделю, часы (число)
# forbidden_categories   — запретные категории через запятую, или none
# forbidden_platforms    — запретные площадки через запятую, или none

capital_usd: <fill>
max_drawdown_pct: <fill>
liquidity_need_usd: <fill>
weekly_time_budget_hours: <fill>
forbidden_categories: <fill>
forbidden_platforms: <fill>
