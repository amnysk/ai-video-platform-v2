.PHONY: help up down logs migrate smoke lint fmt types test test-unit test-integration check

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

smoke:          ## 起動中のスタックに対してEpisodeを1本流す
	./scripts/smoke.sh

lint:
	ruff check .

fmt:
	ruff format .

types:
	pyright

test-unit:
	pytest tests/unit tests/contract tests/architecture

test-integration:
	pytest tests/integration -m integration

test: test-unit

check: lint types test-unit
