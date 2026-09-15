.PHONY: help up down logs migrate smoke smoke-script script-worker lint fmt types test test-unit test-integration test-live check

help:
	@grep -E '^[a-z-]+:' Makefile | cut -d: -f1 | tail -n +2

up:            ## core サービスを起動
	docker compose --profile core up -d

down:
	docker compose --profile core --profile observability down

logs:
	docker compose --profile core logs -f api dummy-worker

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
