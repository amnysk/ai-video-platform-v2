# api と worker の共通イメージ。起動コマンドだけ compose 側で切り替える。
FROM python:3.13-slim AS base

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app

COPY pyproject.toml README.md ./
# 依存だけを先に入れてレイヤキャッシュを効かせる
RUN pip install --no-cache-dir \
      "fastapi>=0.115" "uvicorn[standard]>=0.30" "sqlalchemy[asyncio]>=2.0" \
      "asyncpg>=0.29" "alembic>=1.13" "minio>=7.2" "temporalio>=1.7" \
      "pydantic>=2.8" "pydantic-settings>=2.4"

COPY alembic.ini ./
COPY contracts ./contracts
COPY domain ./domain
COPY infrastructure ./infrastructure
COPY workers ./workers
COPY apps/api ./apps/api
COPY apps/__init__.py ./apps/__init__.py

ENV PYTHONPATH=/app

CMD ["uvicorn", "apps.api.main:app", "--host", "0.0.0.0", "--port", "8000"]
