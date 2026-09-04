"""Codex CLI アダプタの単体テスト。**本物の codex は起動しない**（INV-18）。

CLI 語彙（argv・sandbox・JSONL・exit code）は ``infrastructure`` 側だけの関心事で、
``domain.script.ports`` には漏れない。その境界もここで検査する。
"""

from __future__ import annotations

import ast
import json
import pathlib
from typing import Any

import pytest

from domain.errors import (
    ProviderInvocationError,
    ProviderTimeoutError,
    ProviderUnavailableError,
)
from domain.script.ports import GenerationRequest, GenerationResult
from infrastructure.providers.codex_cli import CodexCliStoryGenerator, resolve_codex_binary
from infrastructure.providers.process import ProcessResult, ProcessTimeout
from prompts import (
    PROMPT_TEMPLATE_ID,
    PROMPT_TEMPLATE_VERSION,
    load_prompt_template,
    render_script_prompt,
)
from tests.support.fakes import FakeProcessRunner, FakeStoryGenerator

REPO = pathlib.Path(__file__).resolve().parents[2]

COMPLETED_JSONL = "\n".join(
    [
        json.dumps({"type": "thread.started", "thread_id": "t1"}),
        json.dumps({"type": "item.completed", "item": {"type": "agent_message"}}),
        json.dumps({"type": "turn.completed", "usage": {"input_tokens": 10}}),
    ]
)


def _writer(payload: str):
    """``--output-last-message`` のファイルを実プロセスの代わりに書く。"""

    def _on_call(argv) -> None:
        argv = list(argv)
        path = pathlib.Path(argv[argv.index("--output-last-message") + 1])
        path.write_text(payload, encoding="utf-8")

    return _on_call


def _generator(runner: FakeProcessRunner, tmp_path: pathlib.Path, **kwargs: Any):
    return CodexCliStoryGenerator(
        binary="/opt/nvm/bin/codex",
        model="gpt-5.1-codex-max",
        runner=runner,
        workspace=tmp_path,
        **kwargs,
    )


def _request(**kwargs: Any) -> GenerationRequest:
    base: dict[str, Any] = {
        "episode_id": "ep-1",
        "prompt": "台本を書け",
        "output_schema": None,
        "timeout_seconds": 900,
    }
    base.update(kwargs)
    return GenerationRequest(**base)


# --- argv / 呼び出し形 ---------------------------------------------------


async def test_generate_returns_the_last_message_file_contents(tmp_path) -> None:
    runner = FakeProcessRunner(stdout=COMPLETED_JSONL, on_call=_writer('{"scenes": []}'))
    result = await _generator(runner, tmp_path).generate(_request())
    assert isinstance(result, GenerationResult)
    assert result.text == '{"scenes": []}'
    assert result.provider_id == "codex"
    assert result.model == "gpt-5.1-codex-max"


async def test_argv_uses_exec_and_never_the_bare_tui(tmp_path) -> None:
    runner = FakeProcessRunner(stdout=COMPLETED_JSONL, on_call=_writer("{}"))
    await _generator(runner, tmp_path).generate(_request())
    argv = runner.argv
    assert argv[0] == "/opt/nvm/bin/codex"
    assert argv[1] == "exec"
    for flag, value in [("-s", "read-only"), ("-C", str(tmp_path)), ("-m", "gpt-5.1-codex-max")]:
        assert argv[argv.index(flag) + 1] == value
    assert "--json" in argv
    assert "--skip-git-repo-check" in argv
    assert "--ephemeral" in argv
    assert "--output-last-message" in argv
    assert argv[-1] == "-", "プロンプトは stdin から渡す"


async def test_ask_for_approval_is_never_passed_to_exec(tmp_path) -> None:
    """``exec`` に ``-a/--ask-for-approval`` は存在しない（付けると exit 2）。"""
    runner = FakeProcessRunner(stdout=COMPLETED_JSONL, on_call=_writer("{}"))
    await _generator(runner, tmp_path, reasoning_effort="high").generate(
        _request(output_schema={"type": "object"})
    )
    assert "--ask-for-approval" not in runner.argv
    assert "-a" not in runner.argv


async def test_prompt_is_passed_on_stdin_not_in_argv(tmp_path) -> None:
    runner = FakeProcessRunner(stdout=COMPLETED_JSONL, on_call=_writer("{}"))
    await _generator(runner, tmp_path).generate(_request(prompt="秘密でない長いプロンプト"))
    call = runner.calls[-1]
    assert call["stdin"] == "秘密でない長いプロンプト"
    assert "秘密でない長いプロンプト" not in " ".join(call["argv"])


async def test_output_schema_is_written_to_a_file_and_passed_by_path(tmp_path) -> None:
    schema = {"type": "object", "properties": {"scenes": {"type": "array"}}}
    seen: dict[str, Any] = {}

    def _on_call(argv) -> None:
        argv = list(argv)
        seen["schema"] = json.loads(
            pathlib.Path(argv[argv.index("--output-schema") + 1]).read_text(encoding="utf-8")
        )
        _writer("{}")(argv)

    runner = FakeProcessRunner(stdout=COMPLETED_JSONL, on_call=_on_call)
    await _generator(runner, tmp_path).generate(_request(output_schema=schema))
    assert seen["schema"] == schema


async def test_no_schema_means_no_output_schema_flag(tmp_path) -> None:
    runner = FakeProcessRunner(stdout=COMPLETED_JSONL, on_call=_writer("{}"))
    await _generator(runner, tmp_path).generate(_request())
    assert "--output-schema" not in runner.argv


async def test_reasoning_effort_is_passed_as_a_config_override(tmp_path) -> None:
    runner = FakeProcessRunner(stdout=COMPLETED_JSONL, on_call=_writer("{}"))
    await _generator(runner, tmp_path, reasoning_effort="high").generate(_request())
    argv = runner.argv
    assert "-c" in argv
    assert argv[argv.index("-c") + 1] == "model_reasoning_effort=high"


async def test_temporary_files_are_cleaned_up(tmp_path) -> None:
    runner = FakeProcessRunner(stdout=COMPLETED_JSONL, on_call=_writer("{}"))
    await _generator(runner, tmp_path).generate(_request(output_schema={"type": "object"}))
    argv = runner.argv
    for flag in ("--output-last-message", "--output-schema"):
        assert not pathlib.Path(argv[argv.index(flag) + 1]).exists()


async def test_request_timeout_is_handed_to_the_process_runner(tmp_path) -> None:
    runner = FakeProcessRunner(stdout=COMPLETED_JSONL, on_call=_writer("{}"))
    await _generator(runner, tmp_path).generate(_request(timeout_seconds=42))
    assert runner.calls[-1]["timeout_seconds"] == 42


# --- 環境変数 -------------------------------------------------------------


async def test_env_is_a_whitelist_and_not_the_whole_environ(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("SOME_UNRELATED_SECRET", "leak-me")
    runner = FakeProcessRunner(stdout=COMPLETED_JSONL, on_call=_writer("{}"))
    await _generator(runner, tmp_path).generate(_request())
    env = runner.calls[-1]["env"]
    assert "SOME_UNRELATED_SECRET" not in env
    assert set(env) <= {"PATH", "HOME", "LANG", "TMPDIR", "XDG_CONFIG_HOME", "CODEX_HOME"}


async def test_path_is_prefixed_with_the_codex_binary_directory(tmp_path) -> None:
    """nvm 配下の codex は PATH に無いことがある。"""
    runner = FakeProcessRunner(stdout=COMPLETED_JSONL, on_call=_writer("{}"))
    await _generator(runner, tmp_path).generate(_request())
    assert runner.calls[-1]["env"]["PATH"].split(":")[0] == "/opt/nvm/bin"


# --- 失敗の変換 -----------------------------------------------------------


async def test_timeout_becomes_provider_timeout_error(tmp_path) -> None:
    runner = FakeProcessRunner(raises=ProcessTimeout("timed out after 900s"))
    with pytest.raises(ProviderTimeoutError):
        await _generator(runner, tmp_path).generate(_request())


@pytest.mark.parametrize(
    "stderr",
    [
        "error: not logged in. Run `codex login`.",
        "Unauthorized: 401 authentication failed",
        "codex: command not found",
        "No such file or directory: codex",
    ],
)
async def test_auth_and_missing_cli_become_provider_unavailable(tmp_path, stderr: str) -> None:
    runner = FakeProcessRunner(returncode=1, stderr=stderr)
    with pytest.raises(ProviderUnavailableError):
        await _generator(runner, tmp_path).generate(_request())


async def test_missing_binary_becomes_provider_unavailable(tmp_path) -> None:
    runner = FakeProcessRunner(raises=FileNotFoundError(2, "No such file or directory"))
    with pytest.raises(ProviderUnavailableError):
        await _generator(runner, tmp_path).generate(_request())


async def test_other_nonzero_exit_becomes_provider_invocation_error(tmp_path) -> None:
    runner = FakeProcessRunner(returncode=7, stderr="stream disconnected before completion")
    with pytest.raises(ProviderInvocationError):
        await _generator(runner, tmp_path).generate(_request())


async def test_exit_zero_without_turn_completed_is_an_invocation_error(tmp_path) -> None:
    """exit code の規約は非公開なので、JSONL の到達点でも判定する。"""
    partial = json.dumps({"type": "thread.started", "thread_id": "t1"})
    runner = FakeProcessRunner(stdout=partial, on_call=_writer('{"scenes": []}'))
    with pytest.raises(ProviderInvocationError):
        await _generator(runner, tmp_path).generate(_request())


async def test_empty_last_message_is_an_invocation_error(tmp_path) -> None:
    runner = FakeProcessRunner(stdout=COMPLETED_JSONL, on_call=_writer("   \n"))
    with pytest.raises(ProviderInvocationError):
        await _generator(runner, tmp_path).generate(_request())


async def test_missing_last_message_file_is_an_invocation_error(tmp_path) -> None:
    runner = FakeProcessRunner(stdout=COMPLETED_JSONL)
    with pytest.raises(ProviderInvocationError):
        await _generator(runner, tmp_path).generate(_request())


# --- 監査ログと secret ----------------------------------------------------


async def test_raw_log_keeps_the_jsonl_for_auditing(tmp_path) -> None:
    runner = FakeProcessRunner(stdout=COMPLETED_JSONL, on_call=_writer("{}"))
    result = await _generator(runner, tmp_path).generate(_request())
    assert "turn.completed" in result.raw_log


@pytest.mark.parametrize(
    "leak",
    [
        "Authorization: Bearer sk-secret-value",
        "OPENAI_API_KEY=sk-secret-value",
        '"access_token": "sk-secret-value"',
    ],
)
async def test_raw_log_masks_secrets(tmp_path, leak: str) -> None:
    noisy = COMPLETED_JSONL + "\n" + json.dumps({"type": "item.completed", "text": leak})
    runner = FakeProcessRunner(stdout=noisy, on_call=_writer("{}"))
    result = await _generator(runner, tmp_path).generate(_request())
    assert "sk-secret-value" not in result.raw_log
    assert "***" in result.raw_log


async def test_error_messages_do_not_carry_secrets(tmp_path) -> None:
    runner = FakeProcessRunner(returncode=9, stderr="OPENAI_API_KEY=sk-secret-value boom")
    with pytest.raises(ProviderInvocationError) as excinfo:
        await _generator(runner, tmp_path).generate(_request())
    assert "sk-secret-value" not in str(excinfo.value)


# --- binary 解決 ----------------------------------------------------------


def test_resolve_codex_binary_prefers_the_configured_path() -> None:
    assert resolve_codex_binary("/explicit/codex") == "/explicit/codex"


def test_resolve_codex_binary_falls_back_to_which(monkeypatch) -> None:
    monkeypatch.setattr(
        "infrastructure.providers.codex_cli.shutil.which", lambda _name: "/from/which/codex"
    )
    assert resolve_codex_binary("") == "/from/which/codex"


def test_resolve_codex_binary_raises_provider_unavailable_when_absent(monkeypatch) -> None:
    monkeypatch.setattr("infrastructure.providers.codex_cli.shutil.which", lambda _name: None)
    with pytest.raises(ProviderUnavailableError):
        resolve_codex_binary("")


# --- ポートの純度（INV-6） ------------------------------------------------


def test_ports_module_is_free_of_cli_vocabulary() -> None:
    source = (REPO / "domain/script/ports.py").read_text(encoding="utf-8")
    for token in (
        "argv",
        "sandbox",
        "output-last-message",
        "turn.completed",
        "exit code",
        "subprocess",
        "codex exec",
        "--json",
    ):
        assert token not in source, f"domain/script/ports.py leaks CLI vocabulary: {token}"


def test_ports_module_imports_nothing_from_infrastructure() -> None:
    tree = ast.parse((REPO / "domain/script/ports.py").read_text(encoding="utf-8"))
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            names = [a.name for a in node.names]
        elif isinstance(node, ast.ImportFrom):
            names = [node.module or ""]
        else:
            continue
        for name in names:
            assert not name.startswith(("infrastructure", "workers", "apps"))


# --- プロンプトテンプレート ------------------------------------------------


def test_prompt_template_lives_in_a_file_not_in_python() -> None:
    assert (REPO / "prompts/script_ja.md").is_file()
    assert PROMPT_TEMPLATE_ID == "script_ja"
    assert PROMPT_TEMPLATE_VERSION == "1"
    init_source = (REPO / "prompts/__init__.py").read_text(encoding="utf-8")
    assert len(init_source) < 4_000, "巨大なプロンプト本文を Python に直書きしない"


def test_prompt_template_forbids_prose_and_code_fences() -> None:
    template = load_prompt_template(PROMPT_TEMPLATE_ID)
    assert "{" in template
    assert "コードフェンス" in template


def test_prompt_template_reserves_injected_fields_for_the_caller() -> None:
    template = load_prompt_template(PROMPT_TEMPLATE_ID)
    for field in ("episode_id", "schema_version", "generator"):
        assert field in template


def test_render_script_prompt_substitutes_the_placeholders() -> None:
    rendered = render_script_prompt(
        topic="応仁の乱",
        language="ja",
        schema_json='{"type": "object"}',
    )
    assert "応仁の乱" in rendered
    assert '{"type": "object"}' in rendered
    assert "{{" not in rendered


def test_load_prompt_template_rejects_path_traversal() -> None:
    with pytest.raises(ValueError):
        load_prompt_template("../infrastructure/config")


# --- フェイク自身 ---------------------------------------------------------


async def test_fake_story_generator_records_requests_and_fails_n_times() -> None:
    fake = FakeStoryGenerator(output=lambda r: r.episode_id, fail_times=1, error=RuntimeError("x"))
    with pytest.raises(RuntimeError):
        await fake.generate(_request())
    result = await fake.generate(_request(episode_id="ep-2"))
    assert result.text == "ep-2"
    assert fake.calls == 2
    assert [r.episode_id for r in fake.requests] == ["ep-1", "ep-2"]


def test_process_result_is_a_plain_value() -> None:
    assert ProcessResult(returncode=0, stdout="a", stderr="b").stdout == "a"


# --- SubprocessRunner（本物のプロセスだが codex ではない） -----------------


async def test_subprocess_runner_passes_stdin_and_captures_streams() -> None:
    from infrastructure.providers.process import SubprocessRunner

    result = await SubprocessRunner().run(
        ["/bin/sh", "-c", "cat; echo err >&2; exit 3"],
        stdin="hello",
        env={"PATH": "/usr/bin:/bin"},
        timeout_seconds=10,
    )
    assert result.returncode == 3
    assert result.stdout == "hello"
    assert "err" in result.stderr


async def test_subprocess_runner_never_uses_a_shell() -> None:
    source = (REPO / "infrastructure/providers/process.py").read_text(encoding="utf-8")
    assert "create_subprocess_shell" not in source
    assert "shell=True" not in source


async def test_subprocess_runner_kills_the_whole_process_group_on_timeout() -> None:
    """codex は MCP サーバ等の子を作る。親だけ kill すると孫が残る。"""
    from infrastructure.providers.process import SubprocessRunner

    marker = pathlib.Path(__import__("tempfile").mkdtemp()) / "child-alive"
    script = f"sh -c 'sleep 30; touch {marker}' & wait"
    with pytest.raises(ProcessTimeout):
        await SubprocessRunner(grace_seconds=0.2).run(
            ["/bin/sh", "-c", script],
            stdin="",
            env={"PATH": "/usr/bin:/bin"},
            timeout_seconds=1,
        )
    assert not marker.exists()


# --- 実環境で判明した挙動（実測 2026-09-04） ---------------------------------
#
# codex exec は **turn が失敗しても exit code 0 を返す**。失敗は stdout の
# JSONL に `{"type":"error",...}` / `{"type":"turn.failed",...}` として現れる。
# exit code に依存した判定は本番でのみ壊れる。


TURN_FAILED_UNSUPPORTED_MODEL = "\n".join(
    [
        json.dumps({"type": "turn.started"}),
        json.dumps(
            {
                "type": "error",
                "message": '{"type":"error","status":400,"error":{"type":'
                '"invalid_request_error","message":"The \'gpt-5.1-codex-max\' model is '
                'not supported when using Codex with a ChatGPT account."}}',
            }
        ),
        json.dumps({"type": "turn.failed", "error": {"message": "model not supported"}}),
    ]
)


async def test_turn_failed_with_exit_zero_is_detected(tmp_path) -> None:
    """exit 0 でも turn.failed があれば生成は成立していない。"""
    generator = _generator(FakeProcessRunner(stdout=TURN_FAILED_UNSUPPORTED_MODEL), tmp_path)
    with pytest.raises((ProviderInvocationError, ProviderUnavailableError)):
        await generator.generate(_request())


async def test_unsupported_model_is_needs_input_not_retryable(tmp_path) -> None:
    """設定の誤りは何度呼んでも直らない。retry予算を浪費せず人間へ回す。"""
    generator = _generator(FakeProcessRunner(stdout=TURN_FAILED_UNSUPPORTED_MODEL), tmp_path)
    with pytest.raises(ProviderUnavailableError):
        await generator.generate(_request())


async def test_turn_failed_message_is_surfaced_for_diagnosis(tmp_path) -> None:
    """失敗の中身がログに出ること。空メッセージだと原因追跡ができない。"""
    generator = _generator(FakeProcessRunner(stdout=TURN_FAILED_UNSUPPORTED_MODEL), tmp_path)
    with pytest.raises((ProviderInvocationError, ProviderUnavailableError)) as excinfo:
        await generator.generate(_request())
    assert "not supported" in str(excinfo.value)


async def test_model_is_not_passed_when_unset(tmp_path) -> None:
    """モデル未指定なら -m を渡さず、ユーザーの codex 設定に従う。

    アカウントで使えないモデルをこちらが決め打ちすると本番でのみ失敗する
    （実測: gpt-5.1-codex-max は ChatGPT アカウントで非対応）。
    """
    runner = FakeProcessRunner(stdout=COMPLETED_JSONL, on_call=_writer('{"ok": true}'))
    generator = CodexCliStoryGenerator(
        binary="/opt/nvm/bin/codex", model="", runner=runner, workspace=tmp_path
    )
    await generator.generate(_request())
    assert "-m" not in runner.argv
