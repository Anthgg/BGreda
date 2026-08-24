# =============================================================================
# Imagen de produccion de BGreda, orientada a Google Cloud Run.
# =============================================================================

# ---- Etapa 1: dependencias --------------------------------------------------
FROM python:3.12-slim AS builder

# uv resuelve e instala desde uv.lock, garantizando builds reproducibles.
COPY --from=ghcr.io/astral-sh/uv:0.12 /uv /usr/local/bin/uv

ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PYTHON_DOWNLOADS=never

WORKDIR /app

# Capa cacheable: solo cambia cuando cambian las dependencias.
COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-dev --no-install-project

COPY app ./app
COPY alembic ./alembic
COPY alembic.ini README.md ./
RUN uv sync --frozen --no-dev

# ---- Etapa 2: runtime -------------------------------------------------------
FROM python:3.12-slim AS runtime

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PATH="/app/.venv/bin:$PATH" \
    PORT=8080 \
    XDG_CACHE_HOME=/tmp/.cache

# Dependencias de sistema minimas para generacion de PDF (WeasyPrint / Pango / Cairo)
# y usuario sin privilegios.
RUN apt-get update && apt-get install -y --no-install-recommends \
    libpango-1.0-0 \
    libpangoft2-1.0-0 \
    libpangocairo-1.0-0 \
    libgdk-pixbuf-2.0-0 \
    shared-mime-info \
    fonts-dejavu-core \
    && rm -rf /var/lib/apt/lists/* \
    && groupadd --system --gid 1001 appuser \
    && useradd --system --uid 1001 --gid 1001 --no-create-home appuser

WORKDIR /app

COPY --from=builder --chown=1001:1001 /app /app

USER 1001:1001

EXPOSE 8080

# Cloud Run inyecta PORT; el shell lo expande en tiempo de arranque.
# --proxy-headers hace que la app respete X-Forwarded-Proto detras del balanceador,
# requisito para que las cookies Secure se emitan correctamente.
CMD ["sh", "-c", "exec uvicorn app.main:app --host 0.0.0.0 --port ${PORT:-8080} --proxy-headers --forwarded-allow-ips='*'"]
