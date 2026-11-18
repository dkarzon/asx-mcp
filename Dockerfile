FROM python:3.13-slim

WORKDIR /app

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    CACHE_DIR=/app/cache \
    HOST=0.0.0.0 \
    PORT=8000

# Install Python dependencies first for better layer caching.
# All dependencies ship prebuilt wheels, so no compiler is needed.
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Run as an unprivileged user
RUN useradd --system --uid 10001 --home-dir /app appuser \
    && mkdir -p /app/cache \
    && chown appuser /app/cache

COPY app/ ./app/
COPY server.py .

USER appuser

VOLUME ["/app/cache"]
EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
    CMD python -c "import os, urllib.request; urllib.request.urlopen(f'http://127.0.0.1:{os.environ[\"PORT\"]}/health', timeout=3)" || exit 1

CMD ["python", "server.py"]
