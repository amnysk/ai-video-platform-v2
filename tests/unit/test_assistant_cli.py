"""``scripts/assistant.py``（ADR-0042 §7）。理由は docs/testing/assistant-rationale.md §5。"""

from __future__ import annotations

import importlib.util
import io
import json
import sys
import uuid
from pathlib import Path
from types import ModuleType

import httpx
import pytest

from contracts.assistant_tools import DEFAULT_URL, TOKEN_ENV, TOOLS, URL_ENV
from contracts.intervention import REQUIRED_SCOPE, CommandType

ROOT = Path(__file__).resolve().parents[2]
TOKEN = "cli-secret-token-value-123"
EP = str(uuid.uuid4())


def _load(name: str) -> ModuleType:
    spec = importlib.util.spec_from_file_location(name, ROOT / "scripts" / f"{name}.py")
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


@pytest.fixture(scope="module")
def cli() -> ModuleType:
    return _load("assistant")


def _run(cli, argv, *, env=None, stdin="", transport=None):
    out, err = io.StringIO(), io.StringIO()
    code = cli.main(
        argv,
        env={} if env is None else env,
        stdin=io.StringIO(stdin),
        stdout=out,
        stderr=err,
        transport=transport,
    )
    return code, out.getvalue(), err.getvalue()


def test_tools_prints_the_catalogue_as_json(cli) -> None:
    code, out, _ = _run(cli, ["tools"])
    assert code == 0
    data = json.loads(out)
    assert [d["name"] for d in data] == [t.name.value for t in TOOLS]
    for entry in data:
        assert set(entry) == {"name", "description", "input_schema", "effect", "required_scope"}
    by_name = {d["name"]: d for d in data}
    apply_scope = by_name["apply_change"]["required_scope"]
    assert apply_scope == REQUIRED_SCOPE[CommandType.APPLY_CHANGE].value
    assert by_name["get_episode_status"]["required_scope"] == "read"
    assert by_name["add_comment"]["effect"] == "annotate"


def test_tools_tool_use_is_the_minimal_shape(cli) -> None:
    code, out, _ = _run(cli, ["tools", "--tool-use"])
    assert code == 0
    assert all(set(d) == {"name", "description", "input_schema"} for d in json.loads(out))


def test_the_env_names_are_the_same_as_intervene_py() -> None:
    intervene = _load("intervene")
    assert intervene.TOKEN_ENV == TOKEN_ENV
    assert intervene.URL_ENV == URL_ENV
    assert intervene.DEFAULT_URL == DEFAULT_URL


def test_call_executes_with_env_config_and_never_prints_the_token(cli) -> None:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, json={"episode_id": EP})

    code, out, err = _run(
        cli,
        [
            "call",
            "get_episode_status",
            "--input",
            json.dumps({"episode_id": EP}),
            "--session",
            "s1",
            "--tool-call-id",
            "c1",
        ],
        env={TOKEN_ENV: TOKEN, URL_ENV: "http://example.test:9000"},
        transport=httpx.MockTransport(handler),
    )
    assert code == 0
    assert json.loads(out) == {"status": "ok", "data": {"episode_id": EP}}
    assert TOKEN not in out and TOKEN not in err
    [request] = seen
    assert str(request.url).startswith(f"http://example.test:9000/v1/intervention/episodes/{EP}")
    assert request.headers["Authorization"] == f"Bearer {TOKEN}"
    assert request.headers["X-Correlation-ID"] == "assistant:s1:c1"


def test_input_can_come_from_stdin(cli) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={})

    code, out, _ = _run(
        cli,
        ["call", "list_change_sets", "--input", "-", "--session", "s", "--tool-call-id", "c"],
        env={TOKEN_ENV: TOKEN},
        stdin=json.dumps({"episode_id": EP}),
        transport=httpx.MockTransport(handler),
    )
    assert code == 0 and json.loads(out)["status"] == "ok"


def test_usage_errors_exit_2_without_a_request(cli) -> None:
    def boom(request: httpx.Request) -> httpx.Response:  # pragma: no cover
        raise AssertionError("no request expected")

    transport = httpx.MockTransport(boom)
    base = ["call", "get_episode_status", "--session", "s", "--tool-call-id", "c"]
    no_token, _, err = _run(cli, [*base, "--input", "{}"], env={}, transport=transport)
    assert no_token == 2 and TOKEN_ENV in err
    bad_json, _, _ = _run(cli, [*base, "--input", "{"], env={TOKEN_ENV: TOKEN}, transport=transport)
    assert bad_json == 2
    invalid, out, _ = _run(
        cli, [*base, "--input", "{}"], env={TOKEN_ENV: TOKEN}, transport=transport
    )
    assert invalid == 2 and json.loads(out)["status"] == "invalid_input"
    assert _run(cli, ["call", "x"])[0] == 2  # 必須引数の不足


def test_refusals_and_no_response_have_distinct_exit_codes(cli) -> None:
    def refused(request: httpx.Request) -> httpx.Response:
        return httpx.Response(404, json={"detail": {"error": "episode_not_found"}})

    def down(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("down")

    argv = [
        "call",
        "get_episode_status",
        "--input",
        json.dumps({"episode_id": EP}),
        "--session",
        "s",
        "--tool-call-id",
        "c",
    ]
    env = {TOKEN_ENV: TOKEN}
    assert _run(cli, argv, env=env, transport=httpx.MockTransport(refused))[0] == 1
    assert _run(cli, argv, env=env, transport=httpx.MockTransport(down))[0] == 3
