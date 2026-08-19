4# OraclePoly: Инструкция по установке (Setup Guide)

Этот документ содержит пошаговое руководство по развертыванию всех компонентов системы OraclePoly.

---

## 1. Системные требования
- **OS:** macOS / Linux / Windows (WSL2)
- **Node.js:** v20+ (рекомендуется v22)
- **Python:** 3.11+
- **Database:** PostgreSQL 16 (asyncpg)
- **Vector DB:** Qdrant (Cloud или Docker)

---

## 2. Переменные окружения (.env)

Создайте файл `.env` в корне проекта на основе `.env.example`.

### Основные ключи:
```env
# PostgreSQL
DATABASE_URL=postgresql://oraclepoly:password@localhost:5432/oraclepoly

# ИИ и Поиск
OPENAI_API_KEY=sk-...           # Для эмбеддингов и анализа
FIRECRAWL_API_KEY=fc-...        # Для основного скрапинга новостей
SERPER_API_KEY=...               # Резервный поиск

# Хранилище (R2 Data Lake)
R2_ACCOUNT_ID=...
R2_ACCESS_KEY_ID=...
R2_SECRET_ACCESS_KEY=...
```

---

## 3. Настройка Telegram Bot 🤖

OraclePoly работает как Telegram Mini App. Для работы валидации и взаимодействия необходимо настроить бота:

1. **Создание бота:** Через [@BotFather](https://t.me/BotFather) получите `TELEGRAM_BOT_TOKEN`.
2. **Настройка Mini App:** В меню бота укажите URL вашего фронтенда (например, через ngrok или Vercel).
3. **Валидация (Backend):** 
   - Добавьте `TELEGRAM_BOT_TOKEN` в `.env`.
   - Backend автоматически валидирует `X-Telegram-Init-Data` во всех API запросах для предотвращения спуфинга.
4. **Ngrok (Локально):** Для тестирования используйте ngrok:
   ```bash
   ngrok http 5173
   ```
   *Важно:* URL фронтенда в Telegram должен совпадать с URL, по которому доступен Mini App.

---

## 4. Запуск компонентов

### Backend (FastAPI + Redis Queue)
```bash
# Установка
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt

# Локальный запуск сервера
uvicorn app.main:app --host 0.0.0.0 --port 8000 --reload

# Запуск Redis Queue worker (обработка транзакций)
python scripts/oracle_worker.py

# Запуск cron jobs (scheduled tasks)
python scripts/cron_runner.py
```

### Frontend (React + Vite)
```bash
cd client
npm install
npm run dev
```

---

## 5. Риски и Безопасность (Risk Management)

### Технические риски:
1. **DDoS и Rate Limiting:**
   - Пул лимитов настроен через `slowapi`. 
   - **Рекомендация:** В продакшене используйте Cloudflare в режиме "Under Attack", если видите аномальный трафик.
2. **Зависимость от API (I/O Bound):**
   - Система критически зависит от доступности OpenAI и Firecrawl.
   - **Решение:** Реализовано кэширование и Serper-fallback для новостей.
3. **Idempotency (Дублирование транзакций):**
    - Polling может перекрывать интервалы.
    - **Решение:** Используется Redis Queue с ключом идемпотентности (`tx_hash + log_index`) и дедупликация в БД перед отправкой событий.

### Безопасность данных:
- **X-Telegram-Init-Data:** Обязательная проверка HMAC-подписи на бэкенде. Никогда не отключайте эту проверку в продакшене.
- **PostgreSQL доступ:** База данных доступна только через localhost (Docker network_mode: host). Внешние подключения закрыты.

---
*Документ актуален на февраль 2026. По вопросам настройки обращайтесь в Dev-чат.*
