# sami studio — генератор изображений в Telegram

Telegram-бот + Mini App для генерации картинок через AIGate. Две модели, пул API-ключей
для многопоточности, live-оценка цены, референсы, история.

- Telegram-бот на `aiogram 3`
- backend API на `FastAPI`
- Mini App на `React + Vite`
- SQLite для пользователей и истории генераций
- AIGate API: `https://api.aigate.shop/v1`

Пользователь подключает свой API-ключ AIGate, генерации списываются с его баланса.
Можно подключить несколько ключей — параллельные запросы распределятся по ним.

## Модели

Две модели на выбор в интерфейсе:

- **GPT IMAGE 2** (`openai/gpt-image-2`) — основная, через `/v1/images/generations` и `/v1/images/edits`.
  Качество `low`/`medium`/`high`, размер `standard`/`2K`/`max` (до 3840px), батч до 16 картинок за вызов.
- **BANANA 2** (`google/gemini-3.1-flash-image-preview`) — Gemini, через `/v1/chat/completions`.
  Размер `1K`/`2K`/`4K`, оплата по токенам, одна картинка за вызов (N картинок = N параллельных вызовов).

Цены BANANA 2 (замерены на AIGate, промпт почти не влияет — цену задаёт размер):

| Размер | ₽ | $ | Токены |
|---|---|---|---|
| 1K | 0.83 ₽ | $0.0090 | ~1120 |
| 2K | 1.24 ₽ | $0.0135 | ~1680 |
| 4K | 1.87 ₽ | $0.0203 | ~2520 |

## Что умеет

- Подключение API-ключей AIGate (несколько — в пул для многопоточности)
- Проверка баланса и статуса пула ключей
- Две модели с переключателем в один тап
- Качество (low/medium/high), формат (png/jpeg/webp), соотношение сторон (8 вариантов),
  размер (standard/2K/4K), количество (до 16)
- Фото-референсы до 16 штук (drag-drop), порядок `@Image1`, `@Image2`… — для обеих моделей
- Live-оценка цены/токенов под текущий выбор
- WebSocket-прогресс генерации, превью, отмена
- История генераций с просмотром и скачиванием
- **Reuse** — подгрузка настроек (промпт, модель, аспект, размер, качество, формат)
  последней генерации или любой картинки из истории
- Параллельные задачи: можно запустить несколько генераций одновременно, разные модели тоже
- Реальная цена после генерации: AIGate отдаёт `cost_usd` в ответе — пишется в job,
  приоритетнее дельты баланса

## Пул ключей и многопоточность

- Несколько ключей пользователя → параллельные чанки распределяются по ним round-robin
- На `429` (rate-limit) ключ уходит в cooldown 30с, запрос ретраится на другом ключе
- На `401`/`403` ключ помечается мёртвым, исключается до следующего подключения
- Cooldown хранится в БД — переживает рестарт, истекает автоматически
- Юзеры с одним ключом не замечают изменений (round-robin по 1 = прежняя логика)

## Структура

```text
sami-studio-v7/
  backend/
    app/
      api_client.py    AIGate-клиенты: GPTImageClient + GeminiImageClient
      api_routes.py    API для Mini App + пул ключей (KeyPool)
      config.py        реестр моделей, цены, хелперы
      database.py      SQLite + пул ключей (api_keys)
      handlers.py      Telegram-бот
      keyboards.py     Кнопки бота
      websocket.py     Прогресс генерации
    main.py            bot + FastAPI в одном процессе
    Dockerfile
    railway.toml
  frontend/
    src/
      App.jsx          интерфейс Mini App
      api.js           HTTP + WebSocket клиент
      index.css        дизайн
    vercel.json
  Dockerfile           сборка фронта + бэка в один образ
  railway.toml
```

## Backend: локальный запуск

```bash
cd backend
python -m venv venv
source venv/bin/activate
pip install -r requirements.txt
cp .env.example .env   # если есть, иначе задать BOT_TOKEN и WEBAPP_URL
python main.py
```

Минимальные переменные окружения:

```env
BOT_TOKEN=123456:telegram_bot_token
WEBAPP_URL=http://localhost:5173
ALLOW_DEV_AUTH=false
```

## Frontend: локальный запуск

```bash
cd frontend
npm install
npm run dev
```

## Railway deploy

Проект запускается одним сервисом на Railway:

- root directory: корень репозитория
- Dockerfile: `Dockerfile` в корне
- frontend собирается внутри Docker, backend отдаёт API, бота и готовый фронт с одного домена

Переменные Railway:

```env
BOT_TOKEN=...
WEBAPP_URL=https://your-app.up.railway.app   # обязательно с https://
ALLOW_DEV_AUTH=false
```

После первого deploy заменить `WEBAPP_URL` на публичный домен Railway и указать его же в BotFather
как Web App URL. Миграции колонок `jobs` (`model`, `aspect`, `size_tier`, `output_format`) и
таблица `api_keys` применяются автоматически при старте.

## Production notes

- `WEBAPP_URL` должен начинаться с `https://` — иначе Telegram режет Web App кнопку
  с ошибкой `Only HTTPS links are allowed`.
- `ALLOW_DEV_AUTH=false` на продакшене.
- Картинки хранятся в `media/` — на Railway ephemeral-диск, после redeploy файлы могут
  потеряться. Для постоянного хранения лучше подключить S3/R2.
- API-ключи хранятся plaintext в `api_keys` (как и `users.api_key`); на фронтенд отдаются
  только последние 4 символа (masked).
