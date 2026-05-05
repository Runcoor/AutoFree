# syntax=docker/dockerfile:1.7

# ─── Stage 1: 构建前端 ──────────────────────────────────────────────────
FROM node:22-alpine AS web
WORKDIR /web

COPY frontend/package.json frontend/package-lock.json* ./
RUN npm ci --no-audit --no-fund

COPY frontend/ ./
RUN npm run build

# ─── Stage 2: Python runtime + Playwright Chromium ──────────────────────
FROM python:3.13-slim AS app

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    DATA_DIR=/app/data \
    PLAYWRIGHT_BROWSERS_PATH=/ms-playwright

# Playwright Chromium 系统依赖
RUN apt-get update && apt-get install -y --no-install-recommends \
      ca-certificates curl \
      libnss3 libnspr4 libatk1.0-0 libatk-bridge2.0-0 libcups2 \
      libdrm2 libdbus-1-3 libxcb1 libxkbcommon0 libxcomposite1 \
      libxdamage1 libxfixes3 libxrandr2 libgbm1 libpango-1.0-0 \
      libcairo2 libasound2 libatspi2.0-0 \
      fonts-liberation fonts-noto-cjk \
    && rm -rf /var/lib/apt/lists/*

# uv (快速 python 包管理器)
RUN curl -LsSf https://astral.sh/uv/install.sh | sh && \
    mv /root/.local/bin/uv /usr/local/bin/uv

WORKDIR /app

# 先 copy lockfile 装依赖,这样改代码不重跑 sync
COPY backend/pyproject.toml backend/uv.lock* ./
RUN uv sync --frozen --no-dev || uv sync --no-dev

# 装 Playwright Chromium
RUN uv run playwright install chromium

# 拷源代码 + alembic
COPY backend/ ./
COPY --from=web /web/dist ./static

# 数据目录
RUN mkdir -p /app/data && chmod 0775 /app/data
VOLUME ["/app/data"]

EXPOSE 8000

CMD ["uv", "run", "uvicorn", "autofree.main:app", "--host", "0.0.0.0", "--port", "8000"]
