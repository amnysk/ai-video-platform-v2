"""FastAPIアプリ。UIの唯一の入口（INV-1）。"""

from __future__ import annotations

from fastapi import FastAPI

from apps.api.routers import episodes


def create_app() -> FastAPI:
    app = FastAPI(
        title="ai-video-platform-v2 API",
        version="0.1.0",
        summary="Episodeの作成と状態参照（Phase 1 縦切り）",
    )
    app.include_router(episodes.router)

    @app.get("/healthz", tags=["ops"])
    async def healthz() -> dict[str, str]:
        return {"status": "ok"}

    return app


app = create_app()
