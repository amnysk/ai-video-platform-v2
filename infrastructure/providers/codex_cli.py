"""Codex CLI を ``StoryGenerator`` として使うアダプタ。

**Codex 固有のすべて**（argv 組み立て・sandbox・``--output-last-message``・
JSONL イベント・exit code の解釈）をこのモジュールに閉じ込める。
``domain.script.ports`` 側にこれらの語彙を漏らさない（INV-6）。

実測した CLI 仕様（``codex-cli 0.153.2``）:

- サブコマンドは ``exec``。``codex`` 単体は TUI に落ちる
- ``-a/--ask-for-approval`` は ``exec`` に**存在しない**（付けると exit 2）
- タイムアウトオプションは無い → Python 側（``ProcessRunner``）で扱う
- exit code の規約は非公開 → **数値で分岐しない**。
  0/非0 + stderr のパターン + JSONL の到達点で判定する
"""

from __future__ import annotations

import json
import os
import re
import shutil
import tempfile
from collections.abc import Mapping
from pathlib import Path

from domain.errors import (
    ProviderInvocationError,
    ProviderTimeoutError,
    ProviderUnavailableError,
)
from domain.script.ports import GenerationRequest, GenerationResult
from infrastructure.providers.process import ProcessResult, ProcessRunner, ProcessTimeout

#: この provider の識別子。予約・成果物メタデータと同じ語を使う。
PROVIDER_ID = "codex"

#: 実行に必要な環境変数だけを通す。``os.environ`` を丸ごと渡さない（AGENTS.md §9）。
ENV_WHITELIST = ("HOME", "LANG", "TMPDIR", "XDG_CONFIG_HOME", "CODEX_HOME")

#: JSONL のこのイベントに到達して初めて「1ターン完走した」とみなす。
TURN_COMPLETED_EVENT = "turn.completed"

#: 「人が直せば回復する」失敗（未認証・CLI不在・権限拒否）の stderr パターン。
UNAVAILABLE_PATTERNS = (
    "not logged in",
    "please run `codex login`",
    "codex login",
    "unauthorized",
    "authentication failed",
    "invalid api key",
    "permission denied",
    "command not found",
    # アカウントで使えないモデルを指定した場合（実測: ChatGPTアカウントでの
    # gpt-5.1-codex-max）。設定の誤りなので何度呼んでも直らない。
    "is not supported when using codex",
    "model is not supported",
    "no such file or directory",
    "executable file not found",
)

#: secret らしき値をログから落とすためのパターン。値だけを伏せ、キー名は残す。
_SECRET_PATTERNS = (
    re.compile(r"(?i)\b(authorization)\b\s*[:=]\s*\"?(bearer\s+)?[\w.\-]+"),
    re.compile(
        r"(?i)\b([A-Z0-9_]*(?:API_KEY|TOKEN|SECRET|PASSWORD)[A-Z0-9_]*)\s*[:=]\s*\"?[^\s\"',]+"
    ),
    re.compile(r"(?i)\"(access_token|refresh_token|id_token|api_key)\"\s*:\s*\"[^\"]*\""),
    re.compile(r"\bsk-[A-Za-z0-9_\-]{4,}"),
)

_REDACTED = "***"


def mask_secrets(text: str) -> str:
    """監査ログ・例外メッセージから secret を落とす。

    キー名は残して値だけ ``***`` にする（何が出かかったかは追跡したいため）。
    """
    masked = text
    for pattern in _SECRET_PATTERNS:
        masked = pattern.sub(lambda m: _mask_match(m), masked)
    return masked


def _mask_match(match: re.Match[str]) -> str:
    if not match.groups():
        return _REDACTED
    key = match.group(1)
    if match.re.pattern.startswith('(?i)\\"'):
        return f'"{key}": "{_REDACTED}"'
    return f"{key}={_REDACTED}"


def resolve_codex_binary(configured: str = "") -> str:
    """codex 実行ファイルの絶対パス。設定 > ``shutil.which`` の順。

    nvm 配下（``~/.nvm/versions/node/*/bin/codex``）は PATH に無いことがあるので、
    設定で明示できる経路を先に見る。
    """
    if configured:
        return configured
    found = shutil.which("codex")
    if not found:
        raise ProviderUnavailableError(
            "codex binary not found. set CODEX_BINARY or put codex on PATH"
        )
    return found


class CodexCliStoryGenerator:
    """``StoryGenerator`` の Codex CLI 実装。"""

    provider_id = PROVIDER_ID

    def __init__(
        self,
        *,
        binary: str,
        model: str,
        runner: ProcessRunner,
        workspace: Path,
        reasoning_effort: str | None = None,
    ) -> None:
        self.binary = binary
        self.model = model
        self.runner = runner
        self.workspace = Path(workspace)
        self.reasoning_effort = reasoning_effort

    async def generate(self, request: GenerationRequest) -> GenerationResult:
        work = Path(tempfile.mkdtemp(prefix=f"codex-{request.episode_id}-"))
        last_message = work / "last-message.txt"
        schema_path: Path | None = None
        if request.output_schema is not None:
            schema_path = work / "output-schema.json"
            schema_path.write_text(
                json.dumps(request.output_schema, ensure_ascii=False), encoding="utf-8"
            )
        try:
            result = await self._invoke(request, last_message, schema_path)
            raw_log = mask_secrets(result.stdout)
            self._check_outcome(result, raw_log)
            text = self._read_last_message(last_message)
            return GenerationResult(
                text=text,
                provider_id=PROVIDER_ID,
                model=self.model or "codex-config-default",
                raw_log=raw_log,
            )
        finally:
            shutil.rmtree(work, ignore_errors=True)

    # --- 呼び出し ---------------------------------------------------------

    async def _invoke(
        self,
        request: GenerationRequest,
        last_message: Path,
        schema_path: Path | None,
    ) -> ProcessResult:
        argv = self._build_argv(last_message, schema_path)
        try:
            return await self.runner.run(
                argv,
                stdin=request.prompt,
                env=self._build_env(),
                timeout_seconds=request.timeout_seconds,
                cwd=str(self.workspace),
            )
        except ProcessTimeout as exc:
            raise ProviderTimeoutError(
                f"codex exec exceeded {request.timeout_seconds}s "
                f"(episode={request.episode_id}); reservation stays unreconciled"
            ) from exc
        except (FileNotFoundError, PermissionError) as exc:
            raise ProviderUnavailableError(f"cannot execute codex binary: {exc.strerror}") from exc

    def _build_argv(self, last_message: Path, schema_path: Path | None) -> list[str]:
        argv = [
            self.binary,
            "exec",
            "--json",
            "--skip-git-repo-check",
            "--ephemeral",
            "-s",
            "read-only",
            "-C",
            str(self.workspace),
            "--output-last-message",
            str(last_message),
        ]
        if self.model:
            # 未指定なら ~/.codex/config.toml の既定に従う。アカウントで使えない
            # モデルをこちらが決め打ちすると本番でのみ失敗する（実測）。
            argv += ["-m", self.model]
        if schema_path is not None:
            argv += ["--output-schema", str(schema_path)]
        if self.reasoning_effort:
            argv += ["-c", f"model_reasoning_effort={self.reasoning_effort}"]
        argv.append("-")  # プロンプトは stdin から（ARG_MAX 回避・ps に本文を出さない）
        return argv

    def _build_env(self) -> Mapping[str, str]:
        """ホワイトリストで組む。PATH は codex の親ディレクトリを前置する（nvm 対策）。"""
        env = {name: os.environ[name] for name in ENV_WHITELIST if name in os.environ}
        binary_dir = str(Path(self.binary).parent)
        inherited = os.environ.get("PATH", "/usr/local/bin:/usr/bin:/bin")
        env["PATH"] = f"{binary_dir}:{inherited}" if binary_dir else inherited
        return env

    # --- 結果の判定 -------------------------------------------------------

    def _check_outcome(self, result: ProcessResult, raw_log: str) -> None:
        stderr = mask_secrets(result.stderr)
        # **exit code の数値に依存しない。** codex exec は turn が失敗しても
        # exit 0 を返す（実測 2026-09-04）。失敗は JSONL のイベントに現れる。
        turn_error = self._turn_error(result.stdout)
        haystack = f"{result.stderr}\n{result.stdout}".lower()

        if turn_error is not None or result.returncode != 0:
            detail = mask_secrets(turn_error or stderr.strip() or raw_log.strip()[-500:])
            if any(pattern in haystack for pattern in UNAVAILABLE_PATTERNS):
                # CLI不在・未認証・使えないモデル等。何度呼んでも直らないので
                # retry予算を浪費せず人間へ回す（needs_input → blocked）。
                raise ProviderUnavailableError(f"codex exec unavailable: {detail[:500]}")
            raise ProviderInvocationError(f"codex exec failed: {detail[:500]}")

        if not self._reached_turn_completed(result.stdout):
            raise ProviderInvocationError(
                f"codex exec produced no {TURN_COMPLETED_EVENT} event: {raw_log.strip()[-500:]}"
            )

    @staticmethod
    def _turn_error(stdout: str) -> str | None:
        """JSONL から失敗イベントのメッセージを取り出す。

        原因（モデル未対応・レート制限など）が分からないと、運用が
        「codex exec failed:」という空メッセージだけを見ることになる。
        """
        for line in stdout.splitlines():
            line = line.strip()
            if not line.startswith("{"):
                continue
            try:
                event = json.loads(line)
            except json.JSONDecodeError:
                continue
            if not isinstance(event, dict):
                continue
            if event.get("type") == "error":
                return str(event.get("message", "codex reported an error"))
            if event.get("type") == "turn.failed":
                error = event.get("error")
                if isinstance(error, dict):
                    return str(error.get("message", "turn failed"))
                return "turn failed"
        return None

    @staticmethod
    def _reached_turn_completed(stdout: str) -> bool:
        for line in stdout.splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                event = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(event, dict) and event.get("type") == TURN_COMPLETED_EVENT:
                return True
        return False

    @staticmethod
    def _read_last_message(last_message: Path) -> str:
        if not last_message.is_file():
            raise ProviderInvocationError("codex exec wrote no last message file")
        text = last_message.read_text(encoding="utf-8")
        if not text.strip():
            raise ProviderInvocationError("codex exec returned an empty last message")
        return text
