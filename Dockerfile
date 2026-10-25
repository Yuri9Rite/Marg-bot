# --- Marginal Telegram Bot ---
# A minimal, production-ready image. The container starts in webhook mode and
# binds to $PORT (Render/Fly/Railway inject it). For polling mode override the
# command with: docker run ... marg-bot python econ_bot.py
FROM python:3.12-slim

# Reproducible, lean, non-root container.
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /app

# Install dependencies first so Docker layer caching speeds up rebuilds.
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Run as an unprivileged user (required by many hardened platforms).
RUN useradd --create-home --shell /usr/sbin/nologin botuser
USER botuser

COPY econ_bot.py .

# Platforms such as Render inject PORT automatically; fall back to 8000 locally.
EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=5s --start-period=15s --retries=3 \
    CMD python -c "import os,urllib.request;urllib.request.urlopen(f'http://127.0.0.1:{os.environ.get(\"PORT\",\"8000\")}/',timeout=4)"

CMD ["sh", "-c", "uvicorn econ_bot:app --host 0.0.0.0 --port ${PORT:-8000}"]
