# Marg-bot 🤖

Telegram-бот для расчёта микроэкономических показателей (python-telegram-bot v22 + FastAPI).

## Возможности

- 📊 Точка безубыточности
- 💰 Маржинальность
- 📈 ROI
- 🏭 Себестоимость единицы продукции
- 🔄 Эластичность спроса
- ⚖️ Прибыль / убыток
- 📉 Средние и предельные издержки
- 🏷️ Наценка и скидка
- ⏳ Срок окупаемости
- 🔧 Операционный рычаг
- 🧮 Расчёт NPV (упрощённый, один год денежного потока)
- 🎯 Расчёт оптимальной цены
- 🕘 История расчётов (в пределах сессии, до 50 записей)

Команды: `/start` — меню, `/help` — справка, `/cancel` — отменить текущий расчёт.

## Деплой на Render (webhook)

1. Создайте **Blueprint** или **Web Service** из этого репозитория (runtime: Docker, `render.yaml` приложен).
2. Добавьте переменные окружения:
   - `BOT_TOKEN` — токен бота от [@BotFather](https://t.me/BotFather) (**никогда не коммитьте его в репозиторий**);
   - `WEBHOOK_SECRET` — случайная строка (в `render.yaml` генерируется автоматически).
3. После деплоя бот сам зарегистрирует вебхук на `RENDER_EXTERNAL_URL/telegram/webhook` (эту переменную Render предоставляет автоматически).
4. Health-check: `GET /` → `{"status":"ok"}`.

## Локальный запуск

```bash
pip install -r requirements.txt

# Режим long polling:
export BOT_TOKEN="<токен>"
python econ_bot.py

# Или режим webhook (как на Render):
export BOT_TOKEN="<токен>" WEBHOOK_SECRET="<секрет>" RENDER_EXTERNAL_URL="https://<туннель>.public.ngrok.io"
uvicorn econ_bot:app --host 0.0.0.0 --port 8000

# Или через Docker:
docker build -t marg-bot .
docker run -e BOT_TOKEN="<токен>" -e PORT=8000 -p 8000:8000 marg-bot
```

## Структура

| Файл | Назначение |
|---|---|
| `econ_bot.py` | Весь бот: калькуляции, хендлеры, FastAPI-вебхук |
| `Dockerfile` | Образ для продакшена |
| `render.yaml` | Blueprint-конфигурация Render |
| `requirements.txt` | Зависимости |
