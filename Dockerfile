FROM python:3.12-slim

WORKDIR /app

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY econ_bot.py .

# Render предоставляет переменную PORT; локально можно задать через -e PORT=8000
CMD ["sh", "-c", "uvicorn econ_bot:app --host 0.0.0.0 --port ${PORT:-8000}"]
