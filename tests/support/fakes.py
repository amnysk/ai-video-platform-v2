"""テスト用のフェイク実装。

``tests/integration/test_episode_workflow.py`` の ``FlakyArtifactStore`` と同じ作法
（コンストラクタで挙動を注入し、呼び出しを属性に記録する）に揃える。
"""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from typing import Any

from domain.script.ports import GenerationRequest, GenerationResult
from infrastructure.providers.process import ProcessResult


class FakeProcessRunner:
    """``ProcessRunner`` のフェイク。argv・stdin・env を記録する。

    ``on_call`` は argv を受け取り、実プロセスの副作用（``--output-last-message``
    のファイル生成など）をテスト側で再現するために使う。
    """

    def __init__(
        self,
        *,
        stdout: str = "",
        stderr: str = "",
        returncode: int = 0,
        raises: BaseException | None = None,
        on_call: Callable[[Sequence[str]], None] | None = None,
    ) -> None:
        self.stdout = stdout
        self.stderr = stderr
        self.returncode = returncode
        self.raises = raises
        self.on_call = on_call
        self.calls: list[dict[str, Any]] = []

    @property
    def argv(self) -> list[str]:
        """最後に呼ばれた argv。1回も呼ばれていなければ空。"""
        return list(self.calls[-1]["argv"]) if self.calls else []

    async def run(
        self,
        argv: Sequence[str],
        *,
        stdin: str,
        env: Mapping[str, str],
        timeout_seconds: int,
        cwd: str | None = None,
    ) -> ProcessResult:
        self.calls.append(
            {
                "argv": list(argv),
                "stdin": stdin,
                "env": dict(env),
                "timeout_seconds": timeout_seconds,
                "cwd": cwd,
            }
        )
        if self.on_call is not None:
            self.on_call(argv)
        if self.raises is not None:
            raise self.raises
        return ProcessResult(returncode=self.returncode, stdout=self.stdout, stderr=self.stderr)


class FakeStoryGenerator:
    """``StoryGenerator`` のフェイク。最初の ``fail_times`` 回だけ失敗する。"""

    def __init__(
        self,
        *,
        output: str | Callable[[GenerationRequest], str] = "{}",
        fail_times: int = 0,
        error: BaseException | None = None,
        on_call: Callable[[GenerationRequest], None] | None = None,
        provider_id: str = "codex",
        model: str = "fake-model",
    ) -> None:
        self.output = output
        self.fail_times = fail_times
        self.error = error
        self.on_call = on_call
        self.provider_id = provider_id
        self.model = model
        self.calls = 0
        self.requests: list[GenerationRequest] = []

    async def generate(self, request: GenerationRequest) -> GenerationResult:
        self.calls += 1
        self.requests.append(request)
        if self.on_call is not None:
            self.on_call(request)
        if self.calls <= self.fail_times:
            raise self.error or RuntimeError("FakeStoryGenerator failure")
        text = self.output(request) if callable(self.output) else self.output
        return GenerationResult(
            text=text,
            provider_id=self.provider_id,
            model=self.model,
            raw_log="",
        )
