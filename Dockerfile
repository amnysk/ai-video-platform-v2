# api / dummy-worker / migrate の共通イメージ。起動コマンドだけ compose 側で切り替える。
FROM python:3.13-slim AS base

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1

WORKDIR /app

# 依存の唯一の宣言元は pyproject.toml（AGENTS.md §8 / ADR-0009）。
# **ここにパッケージ名を書き足さないこと。** 検査:
# tests/contract/test_dependency_single_source.py
COPY pyproject.toml README.md constraints.txt alembic.ini ./
COPY contracts ./contracts
COPY domain ./domain
COPY infrastructure ./infrastructure
COPY workers ./workers
COPY apps/__init__.py ./apps/__init__.py
COPY apps/api ./apps/api

# 版の固定は constraints.txt（宣言と固定の役割を分ける）。
# cache mount を効かせるため PIP_NO_CACHE_DIR / --no-cache-dir は使わない。
RUN --mount=type=cache,target=/root/.cache/pip \
    pip install -c constraints.txt .

# alembic.ini の script_location が相対パスなので、ソースも /app に置いたまま使う。
ENV PYTHONPATH=/app

CMD ["uvicorn", "apps.api.main:app", "--host", "0.0.0.0", "--port", "8000"]
