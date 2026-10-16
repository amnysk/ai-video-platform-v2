"""運用者 UI の静的配信（ADR-0045 §1）。

- ``JPMAS2_APPLICATION_API=false``（既定）なら ``/ui/*`` は application route と同じ
  ``404 feature_disabled``。**ファイルに触れる前**に返す（``require_application_api``）。
- 静的ファイルは ``apps/web/static`` だけ。解決後の実パスがその下に無いものは 404
  （``..``・エンコードされた ``..``・絶対パス・symlink での脱出）。ディレクトリは ``index.html``。
- ``/ui`` 配下の**全応答**（成功・404・405・flag OFF の 404・500）に厳格な CSP 等を付ける
  （インラインの script/style・外部 origin を許さない）。route 内で付けると dependency の拒否・405・
  未処理例外が漏れるため、``UiSecurityHeadersMiddleware``（ASGI、``/ui`` 以外には何もしない）で付ける。
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Annotated

from fastapi import APIRouter, Depends
from fastapi.responses import FileResponse, JSONResponse, RedirectResponse, Response
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from apps.api.routers.application import require_application_api

#: ``apps/web/static``。UI の資産はここにだけ置く
STATIC_DIR = Path(__file__).resolve().parents[2] / "web" / "static"

SECURITY_HEADERS: dict[str, str] = {
    "Content-Security-Policy": (
        "default-src 'self'; script-src 'self'; style-src 'self'; connect-src 'self'; "
        "frame-ancestors 'none'; base-uri 'none'"
    ),
    "X-Content-Type-Options": "nosniff",
    "Referrer-Policy": "no-referrer",
    "Cache-Control": "no-store",
}


_log = logging.getLogger(__name__)


def _is_ui_path(path: str) -> bool:
    return path == "/ui" or path.startswith("/ui/")


class UiSecurityHeadersMiddleware:
    """``/ui`` 配下の全応答に ``SECURITY_HEADERS`` を付ける（他の path には触れない）。

    未処理例外は、応答を始める前なら中身を出さない 500 を自分で返す（ヘッダ付き）。
    例外そのものは ``logging`` に残す。
    """

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http" or not _is_ui_path(scope.get("path", "")):
            await self.app(scope, receive, send)
            return
        started = False
        names = {k.lower().encode("latin-1") for k in SECURITY_HEADERS}

        async def send_with_headers(message: Message) -> None:
            nonlocal started
            if message["type"] == "http.response.start":
                started = True
                kept = [(k, v) for k, v in message.get("headers", []) if k.lower() not in names]
                kept += [
                    (k.lower().encode("latin-1"), v.encode("latin-1"))
                    for k, v in SECURITY_HEADERS.items()
                ]
                message = {**message, "headers": kept}
            await send(message)

        try:
            await self.app(scope, receive, send_with_headers)
        except Exception:
            _log.exception("unhandled error while serving /ui")
            if started:
                raise
            response = JSONResponse(
                {"detail": {"error": "internal_error", "message": "internal server error"}},
                status_code=500,
                headers=SECURITY_HEADERS,
            )
            await response(scope, receive, send)


def get_ui_static_dir() -> Path:
    """テストは override する。"""
    return STATIC_DIR


router = APIRouter(
    prefix="/ui",
    tags=["ui"],
    dependencies=[Depends(require_application_api)],
    include_in_schema=False,
)


def _not_found() -> Response:
    return JSONResponse(
        {"detail": {"error": "not_found", "message": "not found"}},
        status_code=404,
        headers=SECURITY_HEADERS,
    )


def _resolve(root: Path, relative: str) -> Path | None:
    """``root`` の下の通常ファイル。無い・脱出する・読めないものは ``None``。"""
    try:
        base = root.resolve(strict=True)
        candidate = (base / relative.lstrip("/")).resolve(strict=False)
        if not candidate.is_relative_to(base):
            return None
        if candidate.is_dir():
            candidate = candidate / "index.html"
        if candidate.is_file() and candidate.resolve(strict=True).is_relative_to(base):
            return candidate
    except (OSError, ValueError):  # 存在しない root・NUL 文字・長すぎる名前
        return None
    return None


@router.get("")
async def redirect_to_slash() -> Response:
    """``/ui`` → ``/ui/``（index.html の相対 URL の基準を ``/ui/`` にする）。"""
    return RedirectResponse("/ui/", status_code=308, headers=SECURITY_HEADERS)


@router.get("/{path:path}")
async def serve_ui(root: Annotated[Path, Depends(get_ui_static_dir)], path: str = "") -> Response:
    if any(part == ".." for part in path.replace("\\", "/").split("/")):
        return _not_found()
    found = _resolve(root, path)
    if found is None:
        return _not_found()
    return FileResponse(found, headers=SECURITY_HEADERS)
