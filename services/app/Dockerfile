# Subscription database UI (FastAPI). Build context = repo root (see compose.yaml).
FROM python:3.11-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PYTHONPATH=/app

WORKDIR /app

COPY requirements.txt .
RUN pip install -r requirements.txt

COPY alembic.ini .
COPY alembic ./alembic
COPY app ./app
COPY scripts ./scripts

# data/ (workbooks, backups, reports) is written at run time; the compose file mounts a volume here locally.
RUN useradd --create-home --uid 10001 app \
    && mkdir -p /app/data/source /app/data/backups /app/data/reports \
    && chown -R app:app /app/data
USER app

EXPOSE 8001
# Bring the schema to the newest migration, then serve.
CMD ["sh", "-c", "alembic upgrade head && exec uvicorn app.main:app --host 0.0.0.0 --port 8001 --proxy-headers"]
