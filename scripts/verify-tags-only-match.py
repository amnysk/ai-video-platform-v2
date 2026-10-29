#!/usr/bin/env python3
"""タグだけの照合が description のマーカー照合と同じ動画を見つけるかの確認（ADR-0075、INV-110）。

**読み取り専用。所有者が手で実行する。** 本番で ``AVP_UPLOAD_DESCRIPTION_WITHOUT_MARKER`` を
ON にする前の前提条件
（``docs/operations/upload-worker.md`` §「marker なし description への切替」）。

    # fixture（ネットワークに出ない。テストと手順の練習用）
    .venv/bin/python scripts/verify-tags-only-match.py --fixture videos.json

    # 実チャンネル。youtube.readonly だけの refresh token を別に用意する
    # （upload scope 付きの token は拒否される）
    YOUTUBE_CLIENT_ID=... YOUTUBE_CLIENT_SECRET=... \
      .venv/bin/python scripts/verify-tags-only-match.py --live \
      --refresh-token-path ~/.config/avp/ro-token

出力は件数と、不一致の video id だけ。title・description・tags・token は出さない（INV-20）。
終了コード: 0 = 100% 一致（比較対象あり・一覧を読み切った）、1 = 不一致 / 結論なし、
2 = 使い方・scope の誤り。
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import pathlib
import sys

# ``python scripts/verify-tags-only-match.py`` で repo のパッケージを import できるように
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

import httpx  # noqa: E402
from pydantic import SecretStr  # noqa: E402

from infrastructure.youtube.errors import YouTubeError  # noqa: E402
from infrastructure.youtube.oauth import RefreshTokenCredentials  # noqa: E402
from infrastructure.youtube.tags_only_verify import (  # noqa: E402
    DEFAULT_MAX_PAGES,
    VerificationReport,
    VideoSample,
    WriteScopeRefusedError,
    run_live,
    verify,
)

HTTP_TIMEOUT_SECONDS = 30.0


def load_fixture(path: pathlib.Path) -> list[VideoSample]:
    """``[{"video_id", "privacy", "tags": [...], "description"}]`` の JSON（新しい順）。"""
    raw = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(raw, list):
        raise ValueError("fixture must be a JSON array")
    return [
        VideoSample(
            video_id=str(item["video_id"]),
            privacy=str(item.get("privacy", "private")),
            tags=tuple(str(t) for t in item.get("tags", [])),
            description=str(item.get("description", "")),
        )
        for item in raw
    ]


def run_fixture(path: pathlib.Path) -> VerificationReport:
    return verify(load_fixture(path))


async def run_live_check(
    client_id: str,
    client_secret: str,
    refresh_token_path: str,
    *,
    max_pages: int,
    client: httpx.AsyncClient,
) -> VerificationReport:
    credentials = RefreshTokenCredentials.from_file(
        client_id, SecretStr(client_secret), refresh_token_path, client=client
    )
    return await run_live(credentials, client=client, max_pages=max_pages)


async def _main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="read-only tags-only vs description-marker check")
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--fixture", type=pathlib.Path, help="JSON file; no network (dry run)")
    mode.add_argument("--live", action="store_true", help="read the real channel (read-only)")
    parser.add_argument("--refresh-token-path", help="read-only refresh token file (--live)")
    parser.add_argument("--max-pages", type=int, default=DEFAULT_MAX_PAGES)
    args = parser.parse_args(argv)

    try:
        if args.fixture is not None:
            report = run_fixture(args.fixture)
        else:
            client_id = os.environ.get("YOUTUBE_CLIENT_ID", "")
            client_secret = os.environ.get("YOUTUBE_CLIENT_SECRET", "")
            if not (client_id and client_secret and args.refresh_token_path):
                print(
                    "--live needs YOUTUBE_CLIENT_ID, YOUTUBE_CLIENT_SECRET "
                    "and --refresh-token-path",
                    file=sys.stderr,
                )
                return 2
            async with httpx.AsyncClient(timeout=HTTP_TIMEOUT_SECONDS) as client:
                report = await run_live_check(
                    client_id,
                    client_secret,
                    args.refresh_token_path,
                    max_pages=args.max_pages,
                    client=client,
                )
    except WriteScopeRefusedError as exc:
        print(f"refused: {exc}", file=sys.stderr)
        return 2
    except (YouTubeError, ValueError, KeyError, OSError) as exc:
        print(f"failed: {type(exc).__name__}", file=sys.stderr)
        return 2
    for line in report.lines():
        print(line)
    return 0 if report.full_agreement else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(_main()))
