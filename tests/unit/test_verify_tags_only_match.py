"""確認ツール ``scripts/verify-tags-only-match.py`` と ``tags_only_verify.py``（INV-110）。

ネットワークには出ない（fixture と ``httpx.MockTransport`` だけ）。理由は
docs/testing/upload-tags-only-verify-rationale.md。
"""

from __future__ import annotations

import ast
import json
import pathlib
import re
from urllib.parse import urlparse

import httpx
import pytest
from pydantic import SecretStr

from contracts.upload import (
    UPLOAD_DESCRIPTION_WITHOUT_MARKER_ENV,
    description_without_marker_enabled,
    upload_marker,
)
from infrastructure.youtube.oauth import TOKEN_ENDPOINT, TOKENINFO_ENDPOINT, RefreshTokenCredentials
from infrastructure.youtube.tags_only_verify import (
    READONLY_SCOPE,
    VideoSample,
    WriteScopeRefusedError,
    run_live,
    verify,
)
from tests.support.script_loader import load_script_module

REPO = pathlib.Path(__file__).resolve().parents[2]
SCRIPT = REPO / "scripts/verify-tags-only-match.py"
MODULE = REPO / "infrastructure/youtube/tags_only_verify.py"
script = load_script_module("verify_tags_only_match", SCRIPT)

M1 = upload_marker("1" * 64)
M2 = upload_marker("2" * 64)
TITLE_SENTINEL = "SECRET-TITLE-SENTINEL"
BODY_SENTINEL = "SECRET-BODY-SENTINEL"


def _video(
    video_id: str, *, tags: tuple[str, ...] = (), desc_marker: str | None = None, privacy="private"
) -> VideoSample:
    description = f"{BODY_SENTINEL}\n\n{desc_marker}" if desc_marker else BODY_SENTINEL
    return VideoSample(video_id=video_id, privacy=privacy, tags=tags, description=description)


# ------------------------------------------------------------- 判定（純粋関数）


def test_video_with_marker_in_both_agrees() -> None:
    report = verify([_video("a", tags=(M1,), desc_marker=M1)])
    assert report.agree == 1 and report.full_agreement and not report.disagree_ids


def test_legacy_description_only_video_is_a_mismatch() -> None:
    # 旧方式で投稿した動画（タグにマーカーが無い）は、タグだけの照合では見つからない
    report = verify([_video("legacy", desc_marker=M1)])
    assert report.disagree_ids == ["legacy"] and not report.full_agreement


def test_tag_on_a_different_video_is_a_mismatch() -> None:
    report = verify([_video("new", tags=(M1,)), _video("old", desc_marker=M1)])
    assert report.disagree_ids == ["old"]


def test_tags_only_new_style_videos_are_counted_but_not_compared() -> None:
    report = verify([_video("a", tags=(M1,), desc_marker=M1), _video("n", tags=(M2,))])
    assert report.tags_marker_only == 1 and report.with_description_marker == 1
    assert report.full_agreement


def test_non_private_and_markerless_videos_are_not_compared() -> None:
    report = verify(
        [
            _video("pub", tags=(M1,), desc_marker=M1, privacy="public"),
            _video("x"),
            _video("gone", privacy="unavailable"),
        ]
    )
    assert report.non_private_skipped == 2 and report.without_marker == 1
    assert report.with_description_marker == 0 and not report.conclusive


def test_nothing_to_compare_is_inconclusive_not_agreement() -> None:
    assert not verify([]).full_agreement
    assert not verify([_video("n", tags=(M1,))]).full_agreement


def test_a_truncated_listing_is_never_a_conclusion() -> None:
    report = verify([_video("a", tags=(M1,), desc_marker=M1)], truncated=True)
    assert report.agree == 1 and not report.full_agreement
    assert "INCONCLUSIVE" in report.lines()[-1]


def test_partial_line_match_in_description_is_not_a_marker() -> None:
    sample = VideoSample("a", "private", (), f"x{M1}")
    assert verify([sample]).without_marker == 1


# ------------------------------------------------------------- CLI（fixture / dry-run）


def _fixture(tmp_path: pathlib.Path, videos: list[dict]) -> pathlib.Path:
    path = tmp_path / "videos.json"
    path.write_text(json.dumps(videos), encoding="utf-8")
    return path


def _raw(video_id: str, tags: list[str], desc_marker: str | None) -> dict:
    description = f"{BODY_SENTINEL}\n{desc_marker}" if desc_marker else BODY_SENTINEL
    return {
        "video_id": video_id,
        "privacy": "private",
        "title": TITLE_SENTINEL,
        "tags": tags,
        "description": description,
    }


async def test_cli_fixture_full_agreement_exits_zero_and_prints_counts_only(
    tmp_path: pathlib.Path, capsys: pytest.CaptureFixture[str]
) -> None:
    path = _fixture(tmp_path, [_raw("aaaaaaaaaaa", [M1], M1), _raw("bbbbbbbbbbb", [M2], M2)])
    assert await script._main(["--fixture", str(path)]) == 0
    out = capsys.readouterr().out
    assert "result: AGREE (100%)" in out and "with description marker: 2" in out
    assert TITLE_SENTINEL not in out and BODY_SENTINEL not in out and M1 not in out


async def test_cli_fixture_mismatch_exits_one_and_lists_only_video_ids(
    tmp_path: pathlib.Path, capsys: pytest.CaptureFixture[str]
) -> None:
    path = _fixture(tmp_path, [_raw("aaaaaaaaaaa", [M1], M1), _raw("legacy00000", [], M2)])
    assert await script._main(["--fixture", str(path)]) == 1
    out = capsys.readouterr().out
    assert "mismatch video id: legacy00000" in out and "result: DISAGREE" in out
    assert TITLE_SENTINEL not in out and BODY_SENTINEL not in out


async def test_cli_requires_an_explicit_mode(capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(SystemExit) as info:
        await script._main([])
    assert info.value.code == 2


async def test_cli_live_without_credentials_is_a_usage_error_and_calls_nothing(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.delenv("YOUTUBE_CLIENT_ID", raising=False)
    monkeypatch.delenv("YOUTUBE_CLIENT_SECRET", raising=False)
    assert await script._main(["--live"]) == 2
    assert "needs YOUTUBE_CLIENT_ID" in capsys.readouterr().err


# ------------------------------------------------------------- live 経路（MockTransport）


class FakeGoogle:
    """YouTube / OAuth の両方を模す。送られた (method, host) をすべて記録する。"""

    def __init__(self, scope: str, videos: list[dict], *, next_page_forever: bool = False) -> None:
        self.scope = scope
        self.videos = videos
        self.requests: list[tuple[str, str, str]] = []
        self.next_page_forever = next_page_forever

    def __call__(self, request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        self.requests.append((request.method, request.url.host, request.url.path))
        if url.startswith(TOKENINFO_ENDPOINT):  # TOKEN_ENDPOINT の前方一致より先に見る
            return httpx.Response(200, json={"scope": self.scope})
        if url.startswith(TOKEN_ENDPOINT):
            return httpx.Response(200, json={"access_token": "ya29.fake", "expires_in": 3600})
        path = request.url.path
        if path.endswith("/channels"):
            uploads = {"relatedPlaylists": {"uploads": "UU1"}}
            return httpx.Response(200, json={"items": [{"contentDetails": uploads}]})
        if path.endswith("/playlistItems"):
            items = [{"contentDetails": {"videoId": v["id"]}} for v in self.videos]
            body: dict = {"items": items}
            if self.next_page_forever:
                body["nextPageToken"] = "more"
            return httpx.Response(200, json=body)
        if path.endswith("/videos"):
            return httpx.Response(200, json={"items": self.videos})
        return httpx.Response(404)


def _api_video(video_id: str, tags: list[str], desc_marker: str | None) -> dict:
    description = f"{BODY_SENTINEL}\n{desc_marker}" if desc_marker else BODY_SENTINEL
    return {
        "id": video_id,
        "snippet": {"title": TITLE_SENTINEL, "tags": tags, "description": description},
        "status": {"privacyStatus": "private"},
    }


def _credentials(tmp_path: pathlib.Path, client: httpx.AsyncClient) -> RefreshTokenCredentials:
    token = tmp_path / "ro-token"
    token.write_text("1//refresh-xyz", encoding="utf-8")
    token.chmod(0o600)
    return RefreshTokenCredentials.from_file("cid", SecretStr("cs"), token, client=client)


async def test_live_readonly_token_reads_with_get_only_and_reports(tmp_path: pathlib.Path) -> None:
    google = FakeGoogle(READONLY_SCOPE, [_api_video("aaaaaaaaaaa", [M1], M1)])
    async with httpx.AsyncClient(transport=httpx.MockTransport(google)) as client:
        report = await run_live(_credentials(tmp_path, client), client=client, max_pages=3)
    assert report.full_agreement
    youtube = [r for r in google.requests if r[1] == "www.googleapis.com"]
    assert youtube and all(method == "GET" for method, _, _ in youtube)
    assert {path.rsplit("/", 1)[-1] for _, _, path in youtube} == {
        "channels",
        "playlistItems",
        "videos",
    }
    # POST は token の更新と scope 確認（Google の OAuth ホスト）だけ
    oauth_host = urlparse(TOKEN_ENDPOINT).hostname
    assert all(host == oauth_host for m, host, _ in google.requests if m != "GET")


@pytest.mark.parametrize(
    "scope",
    [
        "https://www.googleapis.com/auth/youtube.upload",
        f"{READONLY_SCOPE} https://www.googleapis.com/auth/youtube.upload",
        f"{READONLY_SCOPE} https://www.googleapis.com/auth/youtube",
        f"{READONLY_SCOPE} https://www.googleapis.com/auth/youtube.force-ssl",
        f"{READONLY_SCOPE} https://www.googleapis.com/auth/yt-analytics.readonly",
        "",
    ],
)
async def test_live_refuses_any_token_that_is_not_readonly_only_before_calling_youtube(
    tmp_path: pathlib.Path, scope: str
) -> None:
    google = FakeGoogle(scope, [_api_video("aaaaaaaaaaa", [M1], M1)])
    async with httpx.AsyncClient(transport=httpx.MockTransport(google)) as client:
        with pytest.raises(WriteScopeRefusedError) as info:
            await run_live(_credentials(tmp_path, client), client=client, max_pages=3)
    assert not [r for r in google.requests if r[1] == "www.googleapis.com"]
    assert "upload" not in str(info.value) and "force-ssl" not in str(info.value)


async def test_live_page_limit_is_reported_as_truncated_not_agreement(
    tmp_path: pathlib.Path,
) -> None:
    google = FakeGoogle(
        READONLY_SCOPE, [_api_video("aaaaaaaaaaa", [M1], M1)], next_page_forever=True
    )
    async with httpx.AsyncClient(transport=httpx.MockTransport(google)) as client:
        report = await run_live(_credentials(tmp_path, client), client=client, max_pages=2)
    assert report.truncated and not report.full_agreement


# ------------------------------------------------------------- 書き込み経路が無いこと（静的）

WRITE_NAMES = {"insert", "update", "delete", "put", "patch", "upload", "videos_insert"}


def _calls(path: pathlib.Path) -> list[tuple[str, str]]:
    """(囲む関数名, 呼ばれた属性名 / 関数名) の一覧。"""
    tree = ast.parse(path.read_text(encoding="utf-8"))
    found: list[tuple[str, str]] = []

    def visit(node: ast.AST, scope: str) -> None:
        for child in ast.iter_child_nodes(node):
            inner = (
                child.name if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef)) else scope
            )
            if isinstance(child, ast.Call):
                func = child.func
                if isinstance(func, ast.Attribute) and ast.unparse(func.value) == "sys.path":
                    visit(
                        child, inner
                    )  # ``sys.path.insert`` は import 経路の設定で、書き込みではない
                    continue
                name = func.attr if isinstance(func, ast.Attribute) else getattr(func, "id", "")
                found.append((scope, name))
            visit(child, inner)

    visit(tree, "<module>")
    return found


def _imports(path: pathlib.Path) -> set[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            names.update(a.name for a in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            names.add(node.module)
    return names


@pytest.mark.parametrize("path", [SCRIPT, MODULE], ids=["script", "module"])
def test_verification_code_has_no_write_call_and_never_imports_the_upload_adapter(
    path: pathlib.Path,
) -> None:
    assert not [m for m in _imports(path) if m.startswith("infrastructure.youtube.uploader")]
    assert not [m for m in _imports(path) if m in {"googleapiclient", "requests"}]
    calls = _calls(path)
    assert not [c for c in calls if c[1].lower() in WRITE_NAMES], calls
    # HTTP の送信は GET と、scope 確認の POST（granted_scopes 1か所）だけ
    posts = [c for c in calls if c[1] == "post"]
    assert all(scope == "granted_scopes" for scope, _ in posts), posts
    assert not [c for c in calls if c[1] in {"request", "send", "stream"}]


@pytest.mark.parametrize("path", [SCRIPT, MODULE], ids=["script", "module"])
def test_verification_code_names_no_write_scope(path: pathlib.Path) -> None:
    text = path.read_text(encoding="utf-8")
    scopes = set(re.findall(r"auth/[A-Za-z0-9._-]+", text))
    assert scopes <= {"auth/youtube.readonly"}, scopes
    assert "videos.insert" not in text and "playlistItems.insert" not in text


# ------------------------------------------------------------- 本番の切替は禁止（既定 OFF の固定）


def test_the_description_without_marker_flag_defaults_off() -> None:
    assert description_without_marker_enabled({}) is False


def test_no_deploy_artifact_turns_the_description_without_marker_flag_on() -> None:
    """compose / Dockerfile / deploy / Makefile / .env* にこの flag を書かない（ADR-0075）。"""
    paths = [
        *REPO.glob("compose*.yaml"),
        REPO / "Dockerfile",
        REPO / "Makefile",
        REPO / "alembic.ini",
    ]
    paths += [p for p in (REPO / "deploy").rglob("*") if p.is_file()]
    paths += [p for p in REPO.glob(".env*") if p.is_file()]
    paths += [
        p
        for p in (REPO / "scripts").rglob("*")
        if p.is_file() and p.suffix != ".pyc" and p != SCRIPT  # SCRIPT は docstring で名前を述べる
    ]
    hits = [
        p.relative_to(REPO).as_posix()
        for p in paths
        if UPLOAD_DESCRIPTION_WITHOUT_MARKER_ENV in p.read_text(encoding="utf-8", errors="ignore")
    ]
    assert not hits, hits
