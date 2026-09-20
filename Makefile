# schedule-guard.py を動かす python（temporalio 入りの venv）。例: make deploy-workers PYTHON=.venv/bin/python
PYTHON ?= python

.PHONY: deploy-workers workers-versions help up down logs check-docker-uid worker-dirs worker-build workers-up workers-ps workers-logs migrate smoke smoke-script script-worker lint fmt types test test-unit test-integration test-live check

help:
	@grep -E '^[a-z-]+:' Makefile | cut -d: -f1 | tail -n +2

up: check-docker-uid worker-dirs  ## core サービス（常駐 Worker を含む）を起動
	docker compose --profile core up -d

down:
	docker compose --profile core --profile observability down

logs:
	docker compose --profile core logs -f api dummy-worker

check-docker-uid:  ## AVP_UID=0（rootless 用）を rootful Docker で使うと本物の root になるので止める
	@uid="$${AVP_UID:-$$(sed -n 's/^AVP_UID=//p' .env 2>/dev/null)}"; \
	if [ "$${uid:-1000}" = "0" ] && ! docker info -f '{{.SecurityOptions}}' 2>/dev/null | grep -q rootless; then \
	  echo "NG: AVP_UID=0 は rootless Docker 専用。rootful Docker では AVP_UID/AVP_GID を host の所有者にする" >&2; exit 1; \
	fi

worker-dirs:    ## bind mount 元を所有者権限で先に作る（Docker に root 所有で作らせない）
	install -d -m 700 "$${YOUTUBE_TOKEN_HOST_DIR:-$$HOME/.config/avp}"
	install -d "$${AI_VIDEO_WORK_ROOT:-/mnt/minio-hdd/ai-video-work}"

worker-build:   ## 常駐 Worker 共通イメージ（Dockerfile の worker target）をビルド
	docker compose --profile core build script-worker

workers-up: check-docker-uid worker-dirs  ## core サービスと常駐 Worker を起動し healthy まで待つ
	docker compose --profile core up -d --wait

deploy-workers: check-docker-uid worker-dirs  ## Daily Schedule を maintenance pause で包み、共通イメージを1回ビルドして全アプリサービスを同じ版で作り直す（終了時に必ず解除。運用者の pause は外さない）
	SCHEDULE_GUARD="$(PYTHON) scripts/schedule-guard.py" ./scripts/with-maintenance-pause.sh --reason deploy-workers --ttl 45m -- ./scripts/deploy-workers.sh

workers-versions:  ## 稼働中の全アプリコンテナの image id と git revision。混在・古いイメージなら exit 1
	./scripts/workers-versions.sh

workers-ps:
	docker compose --profile core ps

workers-logs:
	docker compose --profile core logs -f --tail=100 script-worker storyboard-worker production-worker production-image-worker production-voice-worker production-video-worker render-worker upload-worker pipeline-worker

migrate:
	docker compose --profile core run --rm migrate

smoke:          ## 起動中のスタックに対してEpisodeを1本流す（Phase 1 骨組み）
	./scripts/smoke.sh

script-worker:  ## Script Worker をホストプロセスとして起動（Codex CLI を使う）
	./scripts/run-script-worker.sh

smoke-script:   ## 本物のCodexで台本を1本生成する。課金/外部呼び出しあり
	./scripts/smoke-script.sh

lint:
	ruff check .

fmt:
	ruff format .

types:
	pyright

test-unit:
	pytest tests/unit tests/contract tests/architecture

test-integration:  ## TEST_DATABASE_URL（*_test のローカルDB）が必要。DATABASE_URL は読まない（ADR-0021）
	@test -n "$$TEST_DATABASE_URL" || { echo "TEST_DATABASE_URL (*_test) is required"; exit 1; }
	pytest tests/integration -m integration

test-live:      ## 本物のCodexを呼ぶ。所有者の明示操作のみ。CIからは走らない
	AVP_LIVE_CODEX=1 pytest tests/live -m live

test: test-unit

check: lint types test-unit
