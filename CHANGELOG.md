# Changelog: OraclePoly

Все заметные изменения в этом проекте будут фиксироваться в этом файле.
Формат основан на [Keep a Changelog](https://keepachangelog.com/en/1.0.0/).

---

## [0.1.0-beta] - 2026-02-12

### Added
- **Gamification Engine:** Внедрена система XP, Streaks и лидерборд для аналитиков (Conversation 63f2fe89).

## [0.1.1] - 2026-02-14

### Changed
- **Cost Optimization:** Полный отказ от Alchemy Webhooks ($$$) в пользу бесплатного Polymarket API Polling для отслеживания транзакций китов.
- **Microservices:** Новый сервис `PolymarketTradePoller` и Inngest cron job для периодического опроса.
- **Configuration:** Отключение Alchemy Webhooks через переменную `ALCHEMY_WEBHOOK_ENABLED=false`.

- **Whale Segmentation:** Транзакции китов теперь фильтруются по топикам (Politics, Crypto и т.д.).
- **Alchemy Integration:** Полная обработка транзакций с поддержкой множественных событий (transfers) в одной транзакции.
- **Black Swan Agent:** Запущен ИИ-агент на LangGraph для поиска рисков.
- **Production Infrastructure:**
    - Деплой на VPS с Docker Compose и Nginx (Reverse Proxy).
    - SSL-сертификаты (Let's Encrypt) и настройка домена example.com.
    - Интеграция Inngest для фоновых задач и очередей.

### Security & Reliability (Phases 1-4)
- **Phase 1: Security**
    - Валидация `auth_date` в Telegram initData (защита от Replay Attack).
    - Перенос системы Credits с фронтенда на защищенный сервер-сайд (Supabase).
    - Принудительная политика CORS и Rate Limiting (Slowapi) на все эндпоинты.
- **Phase 2: Reliability**
    - Внедрение Circuit Breaker (pybreaker) для внешних API (Polymarket, Firecrawl).
    - Логика Retry с экспоненциальной задержкой (Tenacity).
    - Параллелизация запросов в `Oracle Calculator` (ускорение в ~3 раза).
- **Phase 3: Scalability**
    - Композитные индексы в Postgres для оптимизации тяжелых выборок.
    - OpenAI Fallback Chain: автоматическое переключение моделей при сбоях API.
    - Оптимизация Qdrant: шардирование и использование дискового хранилища для экономии RAM.
- **Phase 4: UX/DX**
    - **Offline-First:** Кэширование данных через TanStack Query + localStorage persistence.
    - **Structured Logging:** Переход на JSON-логи (structlog) для удобного мониторинга в проде.

### Changed
- **Oracle Score Formula:** Улучшен алгоритм расчета с учетом веса экспертности китов.
- **UI/UX:** Редизайн дашборда с живыми обновлениями через Supabase Realtime.
- **Documentation:** Полное обновление документации (README, SETUP, ARCHITECTURE, CJM, CONTRIBUTING) на русском языке.

### Fixed
- Идемпотентность вебхуков Alchemy через Inngest.
- Кэширование запросов к News Scraper для снижения затрат на API.
- Ошибки компиляции TypeScript в продакшн-билде.

### Archived Specs
- **Gamification TS:** Реализована спецификация `GAMIFICATION_TS_DRAFT.md` (XP, Streaks, Leaderboard).
- **Predictions & Whales:** Реализован план `PREDICTIONS_AND_WHALES_PLAN.md` (Topic filtering, Whale linking).
- **Alchemy Migration:** Завершена миграция по гайду `ALCHEMY_MIGRATION.md`.
- **Security Audit:** Выполнен план `SECURITY_FIXES_PLAN.md` (Replay Attack, CORS, Rate Limiting).
- **Gamification Mission Control:** Реализована стратегия `AI_MISSION_CONTROL_GAMIFICATION.md`.

---

## [0.0.1] - 2026-02-05

### Added
- **Core backend:** Базовый API на FastAPI.
- **Sensory Layer:** Первичная интеграция с Alchemy и Firecrawl.
- **DB Schema:** Начальная схема Supabase с поддержкой финансовой точности.

---
*Примечание: Проект находится в стадии активной разработки.*
