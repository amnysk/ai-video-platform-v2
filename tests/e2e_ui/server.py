"""ブラウザ E2E 用に、本物の FastAPI app をプロセス内で起動する（ADR-0045、理由は docs/testing）。

  python -m tests.e2e_ui.server [--flag-off]

標準出力に 1 行 JSON ``{"port": ..., "tokens": {...}}`` を出し、標準入力が閉じられるまで動く。
uvicorn は別 thread、ポートは ephemeral。DB・Temporal・MinIO・有料 API には接続しない。
``/__e2e/*`` はテスト専用の管理口（log / reset / bump）。
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
import threading
import time
import uuid
from typing import Any

import uvicorn
from fastapi import FastAPI

from tests.support.ui_e2e_world import (
    TOKENS,
    RequestLog,
    World,
    build_service,
    principals_json,
)


def build(flag: bool) -> tuple[Any, World, RequestLog]:
    from apps.api.application_service import get_application_service
    from apps.api.auth import parse_principals
    from apps.api.dependencies import get_settings
    from apps.api.main import create_app
    from apps.api.routers import intervention
    from infrastructure.config import Settings

    world = World()
    service = build_service(world)
    app: FastAPI = create_app()
    settings = Settings(_env_file=None, jpmas2_application_api=flag)  # type: ignore[call-arg]
    registry = parse_principals(principals_json())
    app.dependency_overrides[get_settings] = lambda: settings
    app.dependency_overrides[intervention.get_principal_registry] = lambda: registry
    app.dependency_overrides[get_application_service] = lambda: service

    log_holder: dict[str, RequestLog] = {}

    @app.get("/__e2e/log")
    async def get_log() -> list[dict[str, Any]]:
        return log_holder["log"].entries

    @app.post("/__e2e/reset")
    async def reset() -> dict[str, bool]:
        world.reset()
        log_holder["log"].entries.clear()
        return {"ok": True}

    @app.post("/__e2e/clear-log")
    async def clear_log() -> dict[str, bool]:
        log_holder["log"].entries.clear()
        return {"ok": True}

    @app.post("/__e2e/bump/{episode_id}")
    async def bump(episode_id: uuid.UUID) -> dict[str, int]:
        return {"control_revision": world.bump_revision(episode_id)}

    @app.get("/__e2e/accepted")
    async def accepted() -> list[dict[str, Any]]:
        return world.accepted

    wrapped = RequestLog(app, world)
    log_holder["log"] = wrapped
    return wrapped, world, wrapped


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--flag-off", action="store_true")
    args = parser.parse_args()
    asgi, _world, _log = build(flag=not args.flag_off)
    config = uvicorn.Config(asgi, host="127.0.0.1", port=0, log_level="warning", access_log=False)
    server = uvicorn.Server(config)
    thread = threading.Thread(target=lambda: asyncio.run(server.serve()), daemon=True)
    thread.start()
    deadline = time.monotonic() + 20
    while not server.started and time.monotonic() < deadline:
        time.sleep(0.05)
    if not server.started:
        print("server did not start", file=sys.stderr)
        sys.exit(1)
    port = server.servers[0].sockets[0].getsockname()[1]
    print(json.dumps({"port": port, "tokens": TOKENS}), flush=True)
    sys.stdin.read()  # 親が閉じるまで待つ
    server.should_exit = True
    thread.join(timeout=5)


if __name__ == "__main__":
    main()
