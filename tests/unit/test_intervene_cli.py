"""``scripts/intervene.py``（介入 API の CLI）。HTTP だけを呼ぶ薄い道具であることを検査する。

理由は ``docs/testing/intervention-c-rationale.md``。
"""

from __future__ import annotations

import ast
import asyncio
import importlib.util
import io
import json
import pathlib
import sys
import uuid
from typing import Any

import pytest

REPO = pathlib.Path(__file__).resolve().parents[2]
SCRIPT = REPO / "scripts" / "intervene.py"
TOKEN = "cli-secret-token-value-42"
EP = "11111111-2222-3333-4444-555555555555"


def _load():
    spec = importlib.util.spec_from_file_location("intervene_cli", SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


cli = _load()


class FakeTransport:
    def __init__(self, status: int = 202, body: Any = None, raw: bytes | None = None) -> None:
        self.requests: list[dict[str, Any]] = []
        self.status = status
        self.body = body if body is not None else {"status": "received", "status_text": "受付済み"}
        self.raw = raw

    def __call__(self, method: str, url: str, headers: dict[str, str], data: bytes | None):
        self.requests.append(
            {
                "method": method,
                "url": url,
                "headers": headers,
                "json": None if data is None else json.loads(data),
            }
        )
        payload = self.raw if self.raw is not None else json.dumps(self.body).encode()
        return self.status, payload


def _run(argv: list[str], transport, env: dict[str, str] | None = None):
    out, err = io.StringIO(), io.StringIO()
    environment = {"AVP_INTERVENTION_TOKEN": TOKEN} if env is None else env
    code = cli.main(argv, env=environment, transport=transport, stdout=out, stderr=err)
    return code, out.getvalue(), err.getvalue()


def _sent(transport: FakeTransport) -> dict[str, Any]:
    [request] = transport.requests
    return request


# --------------------------------------------------------------------------- 境界


def test_the_cli_imports_only_the_standard_library() -> None:
    """CLI は HTTP API だけを呼ぶ（DB・store・repo のコードを読み込まない）。"""
    tree = ast.parse(SCRIPT.read_text(encoding="utf-8"))
    roots: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            roots |= {a.name.split(".")[0] for a in node.names}
        elif isinstance(node, ast.ImportFrom) and node.module and node.level == 0:
            roots.add(node.module.split(".")[0])
    non_stdlib = {r for r in roots if r not in sys.stdlib_module_names and r != "__future__"}
    assert not non_stdlib, non_stdlib


def test_the_token_comes_only_from_the_environment() -> None:
    transport = FakeTransport()
    code, _, err = _run(["show", EP], transport, env={})
    assert code == 2
    assert "AVP_INTERVENTION_TOKEN" in err
    assert transport.requests == []
    with pytest.raises(SystemExit):
        _run(["--token", TOKEN, "show", EP], transport)


# --------------------------------------------------------------------------- 読み取り


def test_show_reads_the_episode_view_with_a_bearer_token() -> None:
    transport = FakeTransport(status=200, body={"episode_id": EP})
    code, out, _ = _run(["--url", "http://api.test:8000/", "show", EP], transport)
    assert code == 0
    request = _sent(transport)
    assert request["method"] == "GET"
    assert request["url"] == f"http://api.test:8000/v1/intervention/episodes/{EP}"
    assert request["headers"]["Authorization"] == f"Bearer {TOKEN}"
    assert json.loads(out) == {"episode_id": EP}


def test_the_base_url_can_come_from_the_environment() -> None:
    transport = FakeTransport(status=200, body={})
    _run(
        ["status", "abcdefgh1234"],
        transport,
        env={
            "AVP_INTERVENTION_TOKEN": TOKEN,
            "AVP_INTERVENTION_URL": "http://other:9000",
        },
    )
    assert _sent(transport)["url"] == "http://other:9000/v1/intervention/commands/abcdefgh1234"


@pytest.mark.parametrize(("fmt", "query"), [("md", "markdown"), ("json", "json"), ("html", "html")])
def test_packet_writes_the_requested_format_to_a_file(tmp_path, fmt: str, query: str) -> None:
    transport = FakeTransport(status=200, raw="# ReviewPacket\n本文".encode())
    target = tmp_path / f"packet.{fmt}"
    code, out, _ = _run(["packet", EP, "--format", fmt, "-o", str(target)], transport)
    assert code == 0
    assert _sent(transport)["url"].endswith(f"/episodes/{EP}/review-packet?format={query}")
    assert target.read_text(encoding="utf-8") == "# ReviewPacket\n本文"
    assert str(target) in out


def test_diff_shows_a_change_set_or_compares_two_revisions() -> None:
    transport = FakeTransport(status=200, body={"impact": {}})
    assert _run(["diff", EP, "--change-set", "cs-1"], transport)[0] == 0
    assert transport.requests[-1]["url"].endswith(f"/episodes/{EP}/change-sets/cs-1")
    assert _run(["diff", EP, "--a", "r1", "--b", "r2"], transport)[0] == 0
    assert transport.requests[-1]["url"].endswith(f"/episodes/{EP}/revisions/compare?a=r1&b=r2")
    code, _, err = _run(["diff", EP], transport)
    assert code == 2 and "--change-set" in err


def test_status_reads_a_command() -> None:
    transport = FakeTransport(status=200, body={"status": "applied"})
    assert _run(["status", "cmd-12345678"], transport)[0] == 0
    assert _sent(transport)["url"].endswith("/v1/intervention/commands/cmd-12345678")


# --------------------------------------------------------------------------- command


def _envelope(transport: FakeTransport) -> dict[str, Any]:
    request = transport.requests[-1]
    assert request["method"] == "POST"
    assert request["url"].endswith(f"/episodes/{EP}/commands")
    assert request["headers"]["Content-Type"] == "application/json"
    return request["json"]


def test_comment_builds_an_envelope_with_a_fresh_uuid4_command_id() -> None:
    transport = FakeTransport()
    code, _, err = _run(["comment", EP, "--body", "短く", "--segment-id", "s1"], transport)
    assert code == 0
    envelope = _envelope(transport)
    assert envelope["type"] == "comment"
    assert envelope["payload"] == {"anchor": {"segment_id": "s1"}, "body": "短く"}
    assert uuid.UUID(envelope["command_id"]).version == 4
    # 再送できるように id を先に出す
    assert envelope["command_id"] in err
    _run(["comment", EP, "--body", "短く", "--segment-id", "s1"], transport)
    assert (
        transport.requests[0]["json"]["command_id"] != transport.requests[1]["json"]["command_id"]
    )


def test_command_id_and_expected_revisions_can_be_given_for_a_safe_retry() -> None:
    transport = FakeTransport()
    _run(
        [
            "hold",
            EP,
            "--reason",
            "確認",
            "--command-id",
            "retry-0001",
            "--expected-control-revision",
            "3",
            "--expected-target-revision",
            "rev-9",
        ],
        transport,
    )
    envelope = _envelope(transport)
    assert envelope == {
        "command_id": "retry-0001",
        "type": "request_hold",
        "payload": {"reason": "確認"},
        "expected_control_revision": 3,
        "expected_target_revision": "rev-9",
    }


@pytest.mark.parametrize(
    ("argv", "expected_type", "expected_payload"),
    [
        (["hold", EP], "request_hold", {}),
        (
            ["apply", EP, "--change-set", "cs-1", "--max-cost", "0.5"],
            "apply_change",
            {"change_set_id": "cs-1", "max_estimated_cost_usd": 0.5},
        ),
        (["apply", EP, "--change-set", "cs-1"], "apply_change", {"change_set_id": "cs-1"}),
        (
            ["reject", EP, "--change-set", "cs-1", "--reason", "不要"],
            "reject_change",
            {"change_set_id": "cs-1", "reason": "不要"},
        ),
        (["resume", EP], "resume", {}),
        (["resume", EP, "--start-stage", "voice"], "resume", {"start_stage": "voice"}),
        (
            ["release-checkpoint", EP, "--point", "before_upload", "--manifest-id", "m1"],
            "release_checkpoint",
            {"point": "before_upload", "manifest_id": "m1"},
        ),
        (
            ["comment", EP, "--body", "b", "--scene-id", "sc1", "--revision-id", "r1"],
            "comment",
            {"anchor": {"scene_id": "sc1", "revision_id": "r1"}, "body": "b"},
        ),
    ],
)
def test_each_command_maps_to_its_envelope(
    argv: list[str], expected_type: str, expected_payload: dict[str, Any]
) -> None:
    transport = FakeTransport()
    assert _run(argv, transport)[0] == 0
    envelope = _envelope(transport)
    assert envelope["type"] == expected_type
    assert envelope["payload"] == expected_payload


def test_propose_sends_the_payload_file_as_a_propose_change(tmp_path) -> None:
    proposal = {"change": {"kind": "script.edit_scenes", "base_revision_id": "r1", "edits": []}}
    path = tmp_path / "proposal.json"
    path.write_text(json.dumps(proposal), encoding="utf-8")
    transport = FakeTransport()
    assert _run(["propose", EP, "--file", str(path)], transport)[0] == 0
    envelope = _envelope(transport)
    assert envelope["type"] == "propose_change"
    assert envelope["payload"] == proposal


def test_import_posts_the_change_proposal_file_unchanged(tmp_path) -> None:
    file = {"episode_id": EP, "proposals": [{"change": {"kind": "x"}}]}
    path = tmp_path / "proposals.json"
    path.write_text(json.dumps(file), encoding="utf-8")
    transport = FakeTransport(status=202, body={"commands": []})
    assert _run(["import", EP, "--file", str(path)], transport)[0] == 0
    request = _sent(transport)
    assert request["url"].endswith(f"/episodes/{EP}/change-proposals")
    assert request["json"] == file


def test_there_is_no_cli_command_for_budget_schedule_or_visibility() -> None:
    """INV-47 の CLI 側: 予算・Schedule・公開設定・mode の操作を CLI に作らない。"""
    names = set(cli.build_parser()._subparsers._group_actions[0].choices)  # noqa: SLF001
    for word in ("budget", "schedule", "visibility", "privacy", "publish", "mode"):
        assert not any(word in name for name in names), names


# --------------------------------------------------------------------------- 失敗


def test_http_errors_exit_nonzero_and_print_the_detail() -> None:
    transport = FakeTransport(status=409, body={"detail": {"error": "command_id_reused"}})
    code, out, err = _run(["hold", EP, "--command-id", "dup-00001"], transport)
    assert code == 1
    assert "command_id_reused" in out + err
    assert TOKEN not in out + err


def test_a_network_error_tells_how_to_retry_safely() -> None:
    def broken(method, url, headers, data):
        raise cli.TransportError(f"connection refused (token {headers['Authorization']})")

    code, out, err = _run(["hold", EP, "--command-id", "keep-0001"], broken)
    assert code == 3
    assert "--command-id keep-0001" in err
    assert TOKEN not in out + err


def test_hash_token_prints_the_sha256_of_the_env_token() -> None:
    import hashlib

    code, out, _ = _run(["hash-token"], transport=FakeTransport())
    assert code == 0
    assert out.strip() == hashlib.sha256(TOKEN.encode()).hexdigest()
    assert TOKEN not in out


# --------------------------------------------------------------------------- 実 router を通す


async def test_the_cli_round_trips_through_the_real_router(session_factory) -> None:
    """CLI → HTTP（ASGI）→ router → store（SQLite）。担当 A / B の関数は fake。"""
    from httpx import ASGITransport, AsyncClient

    from apps.api.auth import parse_principals, token_sha256
    from tests.unit.test_intervention_api import FakeStarter, _build_app, _seed_episode

    registry = parse_principals(
        json.dumps(
            {
                token_sha256(TOKEN): {
                    "actor_type": "human",
                    "actor_id": "owner",
                    "scopes": ["read", "comment", "hold"],
                }
            }
        )
    )
    app = _build_app(session_factory, registry, FakeStarter(), [], [])
    episode_id = await _seed_episode(session_factory)
    loop = asyncio.get_running_loop()

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:

        def bridge(method, url, headers, data):
            future = asyncio.run_coroutine_threadsafe(
                client.request(method, url, headers=headers, content=data), loop
            )
            response = future.result(timeout=10)
            return response.status_code, response.content

        def run(argv):
            out, err = io.StringIO(), io.StringIO()
            code = cli.main(
                ["--url", "http://test", *argv],
                env={"AVP_INTERVENTION_TOKEN": TOKEN},
                transport=bridge,
                stdout=out,
                stderr=err,
            )
            return code, out.getvalue(), err.getvalue()

        code, out, _ = await asyncio.to_thread(
            run, ["hold", episode_id, "--reason", "確認", "--command-id", "cli-hold-0001"]
        )
        assert code == 0, out
        assert json.loads(out)["status"] == "received"
        code, out, _ = await asyncio.to_thread(run, ["status", "cli-hold-0001"])
        assert code == 0 and json.loads(out)["applied"] is False
        # 同じ id での再送は 200（成功扱い）、違う内容は 409
        code, _, _ = await asyncio.to_thread(
            run, ["hold", episode_id, "--reason", "確認", "--command-id", "cli-hold-0001"]
        )
        assert code == 0
        code, out, err = await asyncio.to_thread(
            run, ["hold", episode_id, "--reason", "別", "--command-id", "cli-hold-0001"]
        )
        assert code == 1 and "command_id_reused" in out + err
