# ProxiTok (Python/FastAPI) - production image
FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PROJECT_ENVIRONMENT=/opt/venv \
    PLAYWRIGHT_BROWSERS_PATH=/ms-playwright \
    PATH="/opt/venv/bin:$PATH"

COPY --from=ghcr.io/astral-sh/uv:latest /uv /uvx /usr/local/bin/

WORKDIR /app

# Dependencies first (cached layer); the "redis" extra enables API_CACHE=redis
COPY pyproject.toml uv.lock README.md ./
RUN uv sync --frozen --no-dev --extra redis --no-install-project

# Chromium + its system libraries (needs root for apt)
RUN playwright install --with-deps chromium \
    && rm -rf /var/lib/apt/lists/* \
    && chmod -R a+rX /ms-playwright

COPY proxitok ./proxitok
COPY templates ./templates
COPY static ./static
COPY LICENSE ./
RUN uv sync --frozen --no-dev --extra redis

RUN useradd --create-home --uid 10001 proxitok \
    && mkdir -p /app/cache \
    && chown -R proxitok:proxitok /app/cache
USER proxitok

EXPOSE 8080
CMD ["uvicorn", "proxitok.main:app", "--host", "0.0.0.0", "--port", "8080", "--proxy-headers", "--forwarded-allow-ips", "*"]
