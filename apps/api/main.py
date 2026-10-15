"""FastAPIアプリ。UIの唯一の入口（INV-1）。"""

from __future__ import annotations

import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI

from apps.api.intervention_logging import InterventionRequestLoggingMiddleware
from apps.api.routers import episodes, intervention, intervention_insights, research
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
    # ADR-0042 §6: /v1/intervention だけ、主体・相関 ID・tool 名つきの intervention.api.request
    app.add_middleware(InterventionRequestLoggingMiddleware)
    app.include_router(episodes.router)
    # ADR-0037 §8.5: Research の受け付け・再開・参照（追加だけ。Episode の API は変えない）
    app.include_router(research.router)
    # JP-MAS-001 / ADR-0041 §8: 介入 API（/v1/intervention）。
    # INTERVENTION_PRINCIPALS 未設定なら全て 503
    app.include_router(intervention.router)
    # ADR-0042 §5: 判断の説明と Episode 横断の判断検索（read のみの GET）
    app.include_router(intervention_insights.router)

    @app.get("/healthz", tags=["ops"])
    async def healthz() -> dict[str, str]:
        return {"status": "ok"}

    return app


app = create_app()
