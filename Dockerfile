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

# ---------------------------------------------------------------------------
# 常駐 Worker 共通イメージ（ADR-0024）。script / storyboard / production* / render / upload / pipeline
# がこの1枚を使い、起動する worker は compose の command で選ぶ。
# base の Python 依存（pyproject.toml 由来）はそのまま引き継ぎ、ここでは**Python 依存を足さない**。
# 秘密（Codex 認証・OAuth token・API キー）はイメージに焼かない。実行時に env / bind mount で渡す。
FROM base AS worker

# Codex CLI の版（host の codex と揃える）。
ARG CODEX_VERSION=0.154.0
# scripts/setup-piper.sh と同じ版であること（tests/contract/test_compose_workers.py が検査）。
ARG PIPER_TTS_VERSION=1.8.0

# script / storyboard が読むプロンプト雛形（base は api 用に持たない）。
COPY prompts ./prompts

# git: storyboard が OpenMontage checkout（ro mount）から固定 commit の blob を読む。
# nodejs/npm: Codex CLI の実行環境。
RUN --mount=type=cache,target=/var/cache/apt,sharing=locked \
    --mount=type=cache,target=/var/lib/apt,sharing=locked \
    rm -f /etc/apt/apt.conf.d/docker-clean \
    && apt-get update \
    && apt-get install -y --no-install-recommends git nodejs npm ca-certificates \
    && git config --system --add safe.directory '*'

RUN --mount=type=cache,target=/root/.npm \
    npm install -g "@openai/codex@${CODEX_VERSION}" \
    && codex --version

# piper-tts は GPL-3.0 のため共有の Python 環境に入れず隔離 venv に置く（ADR-0017）。
RUN --mount=type=cache,target=/root/.cache/pip \
    python -m venv /opt/piper/venv \
    && /opt/piper/venv/bin/pip install --disable-pip-version-check "piper-tts==${PIPER_TTS_VERSION}"

# 非 root 実行用の HOME。compose は user: ${AVP_UID}:${AVP_GID}（既定 1000）で動かし、
# ~/.codex は host から rw mount する。
RUN groupadd --gid 1000 avp \
    && useradd --uid 1000 --gid 1000 --home-dir /home/avp --create-home --shell /usr/sbin/nologin avp \
    && chmod 0775 /home/avp
ENV HOME=/home/avp \
    PIPER_PYTHON=/opt/piper/venv/bin/python \
    CODEX_BINARY=/usr/local/bin/codex
USER 1000:1000

CMD ["python", "-m", "infrastructure.runtime.worker_entry", "workers.dummy.run_worker"]
