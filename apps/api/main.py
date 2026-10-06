"""FastAPIアプリ。UIの唯一の入口（INV-1）。"""

from __future__ import annotations

import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI

from apps.api.routers import episodes, research
from contracts.log_contract import EventName, Outcome
from infrastructure.logging.asgi import RequestLoggingMiddleware
from infrastructure.logging.emit import emit, log_guard

logger = logging.getLogger("avp.api")


@asynccontextmanager
async def _lifespan(app: FastAPI) -> AsyncIterator[None]:
    # ADR-0040: service.started / service.stopped（ログの設定は起動点 apps.api.serve が行う）
    with log_guard():
        emit(
            logger,
            EventName.SERVICE_STARTED,
            logging.INFO,
            "api started",
            outcome=Outcome.STARTED.value,
        )
    try:
        yield
    finally:
        with log_guard():
            emit(
                logger,
                EventName.SERVICE_STOPPED,
                logging.INFO,
                "api stopped",
                outcome=Outcome.SUCCEEDED.value,
            )


def create_app() -> FastAPI:
    app = FastAPI(
        title="ai-video-platform-v2 API",
        version="0.1.0",
        summary="Episodeの作成と状態参照（Phase 1 縦切り）",
        lifespan=_lifespan,
    )
    # 1リクエスト = api.request.completed（uvicorn の access log の置き換え / ADR-0040 §1）
    app.add_middleware(RequestLoggingMiddleware)
    app.include_router(episodes.router)
    # ADR-0037 §8.5: Research の受け付け・再開・参照（追加だけ。Episode の API は変えない）
    app.include_router(research.router)

    @app.get("/healthz", tags=["ops"])
    async def healthz() -> dict[str, str]:
        return {"status": "ok"}

    return app


app = create_app()
