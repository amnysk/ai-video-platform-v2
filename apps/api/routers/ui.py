"""運用者 UI の静的配信（ADR-0045 §1）。

- ``JPMAS2_APPLICATION_API=false``（既定）なら ``/ui/*`` は application route と同じ
  ``404 feature_disabled``。**ファイルに触れる前**に返す（``require_application_api``）。
- 静的ファイルは ``apps/web/static`` だけ。解決後の実パスがその下に無いものは 404
  （``..``・エンコードされた ``..``・絶対パス・symlink での脱出）。ディレクトリは ``index.html``。
- 全応答に厳格な CSP 等を付ける（インラインの script/style・外部 origin を許さない）。
"""

from __future__ import annotations

from pathlib import Path
from typing import Annotated

from fastapi import APIRouter, Depends
from fastapi.responses import FileResponse, JSONResponse, Response

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
@router.get("/{path:path}")
async def serve_ui(root: Annotated[Path, Depends(get_ui_static_dir)], path: str = "") -> Response:
    if any(part == ".." for part in path.replace("\\", "/").split("/")):
        return _not_found()
    found = _resolve(root, path)
    if found is None:
        return _not_found()
    return FileResponse(found, headers=SECURITY_HEADERS)
