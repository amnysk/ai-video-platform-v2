"""テスト用のフェイク実装。

``tests/integration/test_episode_workflow.py`` の ``FlakyArtifactStore`` と同じ作法
（コンストラクタで挙動を注入し、呼び出しを属性に記録する）に揃える。
"""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from typing import Any

from contracts.artifacts import ScriptArtifact, StoryboardVisualKind, extract_json_object
from domain.errors import StoryboardOutputUnparseableError, StoryboardSchemaViolationError
from domain.script.ports import GenerationRequest, GenerationResult
from domain.storyboard.ports import StoryboardRawResult, StoryboardRequest, StoryboardSceneDraft
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
        prepare_error: BaseException | None = None,
    ) -> None:
        self.output = output
        self.prepare_error = prepare_error
        self.prepare_calls = 0
        self.release_calls = 0
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


def interpret_fake_storyboard(
    raw_text: str, script: ScriptArtifact
) -> tuple[StoryboardSceneDraft, ...]:
    """``FakeStoryboardGenerator`` の既定の解釈。決定的で、外部仕様を知らない。

    期待する形: ``{"scenes": [{"script_scene_id", "start_ms", "duration_ms",
    "visual_kind", "visual_description"}, ...]}``。読めなければ unparseable、
    形が違えば schema violation（本物の adapter と同じ失敗クラス）。
    """
    del script  # カバレッジ検査は activity 側（domain）が行う
    try:
        parsed = extract_json_object(raw_text)
    except ValueError as exc:
        raise StoryboardOutputUnparseableError(str(exc)) from exc
    try:
        return tuple(
            StoryboardSceneDraft(
                script_scene_id=str(scene["script_scene_id"]),
                start_ms=int(scene["start_ms"]),
                duration_ms=int(scene["duration_ms"]),
                visual_kind=StoryboardVisualKind(scene["visual_kind"]),
                visual_description=str(scene["visual_description"]),
            )
            for scene in parsed["scenes"]
        )
    except (KeyError, TypeError, ValueError) as exc:
        raise StoryboardSchemaViolationError(f"fake storyboard shape: {exc!r}") from exc


class FakeStoryboardGenerator:
    """``StoryboardGenerator`` のフェイク。最初の ``fail_times`` 回だけ ``generate`` が失敗する。

    ``calls`` は ``generate``（有料呼び出しに相当）の回数、``interpret_calls`` は解釈の回数。
    ``prepare_calls`` / ``release_calls`` は局所資源の確保・解放の回数。
    ``prepare_error`` を渡すと ``prepare`` がそれを送出する（予約より前の局所的な失敗）。
    """

    def __init__(
        self,
        *,
        output: str | Callable[[StoryboardRequest], str] = "{}",
        fail_times: int = 0,
        error: BaseException | None = None,
        interpret: Callable[[str, ScriptArtifact], tuple[StoryboardSceneDraft, ...]] | None = None,
        generator_id: str = "fake-storyboard:fake-model",
        generation_spec_id: str = "fake-spec@1",
        provider_id: str = "fake",
        model: str = "fake-model",
        prepare_error: BaseException | None = None,
    ) -> None:
        self.output = output
        self.prepare_error = prepare_error
        self.prepare_calls = 0
        self.release_calls = 0
        self.fail_times = fail_times
        self.error = error
        self._interpret = interpret or interpret_fake_storyboard
        self._generator_id = generator_id
        self._generation_spec_id = generation_spec_id
        self.provider_id = provider_id
        self.model = model
        self.calls = 0
        self.requests: list[StoryboardRequest] = []
        self.interpret_calls = 0

    @property
    def generator_id(self) -> str:
        return self._generator_id

    @property
    def generation_spec_id(self) -> str:
        return self._generation_spec_id

    async def prepare(self, request: StoryboardRequest) -> None:
        del request
        self.prepare_calls += 1
        if self.prepare_error is not None:
            raise self.prepare_error

    async def release(self, request: StoryboardRequest) -> None:
        del request
        self.release_calls += 1

    async def generate(self, request: StoryboardRequest) -> StoryboardRawResult:
        self.calls += 1
        self.requests.append(request)
        if self.calls <= self.fail_times:
            raise self.error or RuntimeError("FakeStoryboardGenerator failure")
        text = self.output(request) if callable(self.output) else self.output
        return StoryboardRawResult(
            text=text, provider_id=self.provider_id, model=self.model, raw_log=""
        )

    def interpret(self, raw_text: str, script: ScriptArtifact) -> tuple[StoryboardSceneDraft, ...]:
        self.interpret_calls += 1
        return self._interpret(raw_text, script)
