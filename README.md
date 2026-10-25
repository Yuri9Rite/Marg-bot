# Marg-bot — микроэкономический Telegram-бот

Telegram-бот для расчёта микроэкономических показателей (точка безубыточности,
маржинальность, ROI, эластичность спроса, NPV, срок окупаемости и др.),
построенный на **python-telegram-bot v22** + **FastAPI**.

## Возможности

- 13 интерактивных расчётов через inline-меню (`ConversationHandler`, 33 состояния)
- Работает в двух режимах: **webhook** (для PaaS) и **long polling** (для разработки)
- Защита webhook секретным токеном Telegram (`secret_token`)
- Healthcheck-эндпоинты для балансировщиков нагрузки
- Ограниченная история расчётов, разбиение длинных сообщений, глобальный error-handler

## Конфигурация (переменные окружения)

| Переменная | Обязательная | Описание |
|---|---|---|
| `BOT_TOKEN` | ✅ | Токен бота от [@BotFather](https://t.me/BotFather) |
| `MODE` | — | `webhook` (по умолчанию) или `polling` |
| `RENDER_EXTERNAL_URL` / `WEBHOOK_URL` | в webhook-режиме | Публичный базовый URL сервиса (Render задаёт сам) |
| `WEBHOOK_SECRET` | рекомендуется | Секрет, который Telegram возвращает в заголовке `X-Telegram-Bot-Api-Secret-Token` |
| `WEBHOOK_PATH` | — | Путь webhook (по умолчанию `/telegram/webhook`) |
| `PORT` | — | Порт HTTP-сервера (его платформы задают автоматически) |
| `LOG_LEVEL` | — | Уровень логирования (по умолчанию `INFO`) |

Скопируйте `.env.example` в `.env` и заполните значения.

## Запуск локально (polling — не нужен публичный URL)

```bash
pip install -r requirements.txt
export BOT_TOKEN="123:your-token"
export MODE=polling
python econ_bot.py            # или: python -m econ_bot
```

## Деплой

### Render (рекомендуемый способ)
В репозитории есть `render.yaml` (Blueprint): подключите репозиторий на
[render.com/new?newType=blueprint](https://render.com/blueprints), задайте
`BOT_TOKEN` — `WEBHOOK_SECRET` сгенерируется автоматически. Сервис стартует
командой `uvicorn econ_bot:app --host 0.0.0.0 --port $PORT`, webhook
регистрируется сам из `RENDER_EXTERNAL_URL`.

### Любой хостинг с Docker
```bash
docker build -t marg-bot .
docker run -p 8000:8000 -e BOT_TOKEN=... -e WEBHOOK_URL=https://your.domain \
  -e WEBHOOK_SECRET=... marg-bot
# polling-вариант:
docker run -e BOT_TOKEN=... -e MODE=polling marg-bot python econ_bot.py
```

### Heroku / Railway / Fly.io
В репозитории есть готовые `Procfile`, `railway.json` и `fly.toml`. Для Fly:
```bash
fly secrets set BOT_TOKEN=... WEBHOOK_SECRET=... WEBHOOK_URL=https://<app>.fly.dev
fly deploy
```

### Свой сервер (systemd/nginx и т.п.)
```bash
BOT_TOKEN=... MODE=webhook WEBHOOK_URL=https://bot.example.com \
WEBHOOK_SECRET=$(openssl rand -hex 32) \
uvicorn econ_bot:app --host 0.0.0.0 --port 8000
```

## HTTP-эндпоинты (webhook-режим)

| Метод | Путь | Назначение |
|---|---|---|
| `GET` | `/` | Liveness-проба (`{"status":"ok"}`) |
| `GET` | `/healthz` | Readiness-проба (бот готов?) |
| `POST` | `/telegram/webhook` | Updates от Telegram (защищён secret-token) |

Swagger-документация доступна на `/docs`.

## Тесты

```bash
pip install pytest
python -m pytest tests/ -q
```

## Структура

```
econ_bot.py           # весь бот: калькуляции, хендлеры, FastAPI-приложение
Dockerfile            # slim-образ, non-root, healthcheck, bind на $PORT
docker-compose.yml    # локальный стек (polling + webhook профили)
render.yaml           # Render Blueprint
Procfile              # Heroku/Stanza
railway.json          # Railway
fly.toml              # Fly.io
requirements.txt      # закреплённые версии зависимостей
.env.example          # шаблон конфигурации
tests/                # unit-тесты калькуляций
```
