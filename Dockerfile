FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PORT=5000 \
    DRIFT_HOST=0.0.0.0 \
    DRIFT_DOWNLOAD_ROOT=/data \
    DRIFT_DB_PATH=/data/drift.sqlite3

WORKDIR /app

# FFmpeg is required for media merging and MP4 compatibility.
RUN apt-get update \
    && apt-get install -y --no-install-recommends ffmpeg ca-certificates \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt ./
RUN python -m pip install --no-cache-dir --upgrade pip \
    && python -m pip install --no-cache-dir -r requirements.txt

COPY drift.py ./
COPY public ./public

# Keep application files read-only to the runtime user; only /data is writable.
RUN useradd --system --uid 10001 --create-home drift \
    && mkdir -p /data \
    && chown drift:drift /data

USER drift

EXPOSE 5000

HEALTHCHECK --interval=30s --timeout=5s --start-period=60s --retries=3 \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:' + __import__('os').environ.get('PORT', '5000') + '/api/health', timeout=3)" || exit 1

CMD ["python", "drift.py"]
