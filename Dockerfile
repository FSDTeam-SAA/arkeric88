# Build stage: install the locked dependencies into a virtualenv with uv.
FROM ghcr.io/astral-sh/uv:python3.13-bookworm-slim AS builder

ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PYTHON_DOWNLOADS=0

WORKDIR /app
COPY pyproject.toml uv.lock .python-version ./
RUN uv sync --locked --no-dev --no-install-project


# Runtime stage: only the virtualenv and the application code.
FROM python:3.13-slim-bookworm

ENV PATH="/app/.venv/bin:$PATH" \
    PYTHONUNBUFFERED=1 \
    SESSION_DB_PATH=/app/storage/sessions.sqlite3

RUN useradd --create-home --uid 1000 appuser

WORKDIR /app
COPY --from=builder /app/.venv ./.venv
COPY main.py ./
COPY app ./app
COPY src ./src
COPY data ./data

# Sessions live outside data/ so a mounted volume can never hide the catalogue
# CSVs shipped in the image, or keep serving old ones after an update.
RUN mkdir -p storage && chown appuser:appuser storage

USER appuser
EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=10s --start-period=60s --retries=3 \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/openapi.json', timeout=5)"

# One worker only: sessions are cached in process memory, so a second worker
# would keep serving its own stale copy of a session the first one updated.
CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8000"]
