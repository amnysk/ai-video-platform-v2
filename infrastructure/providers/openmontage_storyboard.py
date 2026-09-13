"""OpenMontage の手順書・スキーマに誘導された storyboard 生成器（ADR-0016）。

OpenMontage は実行可能なパイプラインを持たず「コーディングアシスタントが LLM」という
使い方をする。ここでは**固定 commit の blob だけ**（手順書 + scene_plan / script スキーマ）を
読み、既存の ``StoryGenerator``（Codex）に渡して scene plan を書かせる。

- OpenMontage の Python は import しない。検証は自前の ``jsonschema`` で行う
- 作業ツリーは読まない（他者の未コミット変更がある）。``git show <commit>:<path>`` のみ
- OpenMontage の語彙はこのモジュールに閉じる（domain には漏らさない）
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from jsonschema import Draft202012Validator

from contracts.artifacts import (
    ScriptArtifact,
    StoryboardScene,
    StoryboardVisualKind,
    extract_json_object,
)
from domain.errors import (
    GenerationSpecUnavailableError,
    StoryboardInputInvalidError,
    StoryboardOutputUnparseableError,
    StoryboardSchemaViolationError,
    WorkspaceUnavailableError,
)
from domain.script.ports import GenerationRequest, StoryGenerator
from domain.storyboard.ports import StoryboardRawResult, StoryboardRequest, StoryboardSceneDraft
from infrastructure.providers.process import ProcessRunner, ProcessTimeout
from infrastructure.workdir import WorkDirectory
from prompts import render_storyboard_prompt

logger = logging.getLogger(__name__)

PROVIDER_ID = "codex"
GENERATOR_KIND = "openmontage-guided"

SCENE_DIRECTOR_PATH = "skills/pipelines/explainer/scene-director.md"
SCENE_PLAN_SCHEMA_PATH = "schemas/artifacts/scene_plan.schema.json"
SCRIPT_SCHEMA_PATH = "schemas/artifacts/script.schema.json"

#: scene_plan の ``scenes[].type`` → platform の語彙。
VISUAL_KIND_BY_SCENE_TYPE: dict[str, StoryboardVisualKind] = {
    "talking_head": StoryboardVisualKind.TALKING_HEAD,
    "broll": StoryboardVisualKind.BROLL,
    "animation": StoryboardVisualKind.ANIMATION,
    "character_scene": StoryboardVisualKind.CHARACTER,
    "diagram": StoryboardVisualKind.DIAGRAM,
    "text_card": StoryboardVisualKind.TEXT_CARD,
    "transition": StoryboardVisualKind.TRANSITION,
    "generated": StoryboardVisualKind.GENERATED,
    "screen_recording": StoryboardVisualKind.SCREEN_RECORDING,
}

#: scene_plan のフィールド → (StoryboardSceneDraft のフィールド)。上限は契約から取る。
_TEXT_FIELDS = {
    "description": "visual_description",
    "framing": "framing",
    "movement": "camera_movement",
    "transition_in": "transition_in",
}

_MAX_REPORTED_ERRORS = 5
_MAX_ERROR_MESSAGE_CHARS = 200


@dataclass(frozen=True, slots=True)
class OpenMontageSpec:
    """固定 commit から読んだ生成仕様。``generation_spec_id`` は内容由来。"""

    commit: str
    scene_director_md: str
    scene_plan_schema: dict[str, Any]
    script_schema: dict[str, Any]
    generation_spec_id: str


def compute_generation_spec_id(commit: str, blobs: dict[str, str]) -> str:
    """commit と blob の内容（パス付き）から同一性を作る。blob が1バイト変われば変わる。"""
    digest = hashlib.sha256()
    for path in sorted(blobs):
        content = blobs[path].encode("utf-8")
        digest.update(f"{path}\0{len(content)}\0".encode())
        digest.update(content)
    return f"openmontage@{commit[:12]}/sha256:{digest.hexdigest()[:16]}"


async def _git(
    runner: ProcessRunner, repo_path: str | Path, args: list[str], timeout_seconds: int
) -> str:
    argv = ["git", "-C", str(repo_path), *args]
    env = {
        "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
        "LANG": "C.UTF-8",
        "GIT_CONFIG_NOSYSTEM": "1",
        "GIT_TERMINAL_PROMPT": "0",
    }
    try:
        result = await runner.run(argv, stdin="", env=env, timeout_seconds=timeout_seconds)
    except (OSError, ProcessTimeout) as exc:
        raise GenerationSpecUnavailableError(f"git {' '.join(args)} failed: {exc}") from exc
    if result.returncode != 0:
        raise GenerationSpecUnavailableError(
            f"git {' '.join(args)} exited {result.returncode}: {result.stderr.strip()[:300]}"
        )
    return result.stdout


def _parse_schema(path: str, text: str) -> dict[str, Any]:
    try:
        schema = json.loads(text)
        if not isinstance(schema, dict):
            raise ValueError("schema is not a JSON object")
        Draft202012Validator.check_schema(schema)
    except Exception as exc:
        raise GenerationSpecUnavailableError(f"invalid JSON schema at {path}: {exc}") from exc
    return schema


async def load_openmontage_spec(
    *, repo_path: str | Path, commit: str, runner: ProcessRunner, timeout_seconds: int = 30
) -> OpenMontageSpec:
    """固定 commit の3つの blob を読む。どの失敗も ``GenerationSpecUnavailableError``。"""
    if not commit or commit.startswith("-"):
        raise GenerationSpecUnavailableError(f"invalid OpenMontage commit: {commit!r}")
    resolved = (
        await _git(
            runner, repo_path, ["rev-parse", "--verify", f"{commit}^{{commit}}"], timeout_seconds
        )
    ).strip()
    if not resolved:
        raise GenerationSpecUnavailableError(f"cannot resolve OpenMontage commit {commit!r}")
    blobs: dict[str, str] = {}
    for path in (SCENE_DIRECTOR_PATH, SCENE_PLAN_SCHEMA_PATH, SCRIPT_SCHEMA_PATH):
        blobs[path] = await _git(runner, repo_path, ["show", f"{resolved}:{path}"], timeout_seconds)
    if not blobs[SCENE_DIRECTOR_PATH].strip():
        raise GenerationSpecUnavailableError(f"empty blob at {SCENE_DIRECTOR_PATH}")
    return OpenMontageSpec(
        commit=resolved,
        scene_director_md=blobs[SCENE_DIRECTOR_PATH],
        scene_plan_schema=_parse_schema(SCENE_PLAN_SCHEMA_PATH, blobs[SCENE_PLAN_SCHEMA_PATH]),
        script_schema=_parse_schema(SCRIPT_SCHEMA_PATH, blobs[SCRIPT_SCHEMA_PATH]),
        generation_spec_id=compute_generation_spec_id(resolved, blobs),
    )


def _seconds(ms: int) -> float | int:
    return ms // 1000 if ms % 1000 == 0 else ms / 1000


def to_openmontage_script(script: ScriptArtifact) -> dict[str, Any]:
    """``ScriptArtifact`` → OpenMontage の script 形。映像メモは自由形式の ``metadata`` に置く。"""
    sections: list[dict[str, Any]] = []
    cursor = 0
    for scene in script.scenes:
        sections.append(
            {
                "id": scene.id,
                "text": scene.narration,
                "start_seconds": _seconds(cursor),
                "end_seconds": _seconds(cursor + scene.duration_ms),
            }
        )
        cursor += scene.duration_ms
    return {
        "version": "1.0",
        "title": script.title,
        "total_duration_seconds": _seconds(script.total_duration_ms),
        "sections": sections,
        "metadata": {
            "language": script.language,
            "hook": script.hook,
            "visual_notes": {scene.id: scene.visual for scene in script.scenes},
        },
    }


def _schema_errors(schema: dict[str, Any], instance: Any) -> list[str]:
    errors = sorted(
        Draft202012Validator(schema).iter_errors(instance),
        key=lambda e: (list(map(str, e.absolute_path)), e.message),
    )
    return [
        f"/{'/'.join(map(str, e.absolute_path))}: {e.message[:_MAX_ERROR_MESSAGE_CHARS]}"
        for e in errors
    ]


def _dump(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True)


class OpenMontageGuidedStoryboardGenerator:
    """``domain.storyboard.ports.StoryboardGenerator`` の実装。"""

    def __init__(
        self,
        *,
        llm: StoryGenerator,
        spec: OpenMontageSpec,
        workdir: WorkDirectory,
        model_label: str,
    ) -> None:
        self._llm = llm
        self._spec = spec
        self._workdir = workdir
        self._model_label = model_label

    @property
    def generator_id(self) -> str:
        return f"{GENERATOR_KIND}:{PROVIDER_ID}:{self._model_label}"

    @property
    def generation_spec_id(self) -> str:
        return self._spec.generation_spec_id

    async def prepare(self, request: StoryboardRequest) -> None:
        """台本を変換・検証し、作業領域を作って監査用の入力を書く。LLM は呼ばない。"""
        converted = self._convert(request)
        job = self._workdir.create(request.episode_id, request.job_id)
        try:
            (job.input / "script.openmontage.json").write_text(_dump(converted), encoding="utf-8")
            (job.openmontage / "scene-director.md").write_text(
                self._spec.scene_director_md, encoding="utf-8"
            )
            (job.openmontage / "scene_plan.schema.json").write_text(
                _dump(self._spec.scene_plan_schema), encoding="utf-8"
            )
        except OSError as exc:
            raise WorkspaceUnavailableError(
                f"cannot write storyboard inputs to {job.base}: {exc}"
            ) from exc

    async def generate(self, request: StoryboardRequest) -> StoryboardRawResult:
        """プロンプトを組んで LLM を1回呼び、生出力を作業領域にも書く。後片付けはしない。"""
        converted = self._convert(request)
        script_json = _dump(converted)
        schema_json = _dump(self._spec.scene_plan_schema)
        prompt = render_storyboard_prompt(
            spec_markdown=self._spec.scene_director_md,
            output_schema_json=schema_json,
            script_json=script_json,
            total_duration_seconds=str(converted["total_duration_seconds"]),
            language=request.script.language,
        )
        result = await self._llm.generate(
            GenerationRequest(
                episode_id=request.episode_id,
                prompt=prompt,
                output_schema=None,
                timeout_seconds=request.timeout_seconds,
            )
        )
        try:
            # 冪等な再作成。呼び出し側は生出力を MinIO に保存するまで作業領域を消さない。
            job = self._workdir.create(request.episode_id, request.job_id)
            (job.output / "scene_plan.raw.txt").write_text(result.text, encoding="utf-8")
        except (OSError, WorkspaceUnavailableError):
            # 作業コピーは source of truth ではない。有料の結果を捨てないために握りつぶす。
            logger.warning(
                "failed to write storyboard raw output copy episode=%s job=%s",
                request.episode_id,
                request.job_id,
                exc_info=True,
            )
        return StoryboardRawResult(
            text=result.text,
            provider_id=result.provider_id,
            model=result.model,
            raw_log=result.raw_log,
        )

    async def release(self, request: StoryboardRequest) -> None:
        """作業領域を消す。失敗しても例外を投げない（ログに残す）。"""
        try:
            self._workdir.cleanup(request.episode_id, request.job_id)
        except Exception:
            logger.warning(
                "failed to clean up storyboard work directory episode=%s job=%s",
                request.episode_id,
                request.job_id,
                exc_info=True,
            )

    def _convert(self, request: StoryboardRequest) -> dict[str, Any]:
        converted = to_openmontage_script(request.script)
        errors = _schema_errors(self._spec.script_schema, converted)
        if errors:
            raise StoryboardInputInvalidError(
                "converted script violates pinned script schema: "
                + "; ".join(errors[:_MAX_REPORTED_ERRORS])
            )
        return converted

    def interpret(self, raw_text: str, script: ScriptArtifact) -> tuple[StoryboardSceneDraft, ...]:
        try:
            plan = extract_json_object(raw_text)
        except ValueError as exc:
            raise StoryboardOutputUnparseableError(str(exc)) from exc

        errors = _schema_errors(self._spec.scene_plan_schema, plan)
        if errors:
            shown = "; ".join(errors[:_MAX_REPORTED_ERRORS])
            more = f" (+{len(errors) - _MAX_REPORTED_ERRORS} more)" if len(errors) > 5 else ""
            raise StoryboardSchemaViolationError(f"scene plan schema violation: {shown}{more}")

        scenes = plan.get("scenes")
        if not isinstance(scenes, list) or not scenes:
            raise StoryboardSchemaViolationError("scene plan has no scenes")
        section_ids = {scene.id for scene in script.scenes}
        # 時間軸の正規化はドメイン規則で、呼び出し側（activity）が一度だけ適用する。
        return tuple(
            self._to_draft(index, scene, section_ids) for index, scene in enumerate(scenes)
        )

    @staticmethod
    def _to_draft(index: int, scene: dict[str, Any], section_ids: set[str]) -> StoryboardSceneDraft:
        label = f"scene {index + 1}"
        section_id = scene.get("script_section_id")
        if not isinstance(section_id, str) or not section_id:
            raise StoryboardSchemaViolationError(f"{label}: script_section_id is required")
        if section_id not in section_ids:
            raise StoryboardSchemaViolationError(
                f"{label}: unknown script_section_id {section_id!r}"
            )
        kind = VISUAL_KIND_BY_SCENE_TYPE.get(scene.get("type", ""))
        if kind is None:
            raise StoryboardSchemaViolationError(
                f"{label}: unknown scene type {scene.get('type')!r}"
            )

        texts: dict[str, str | None] = {}
        for source, target in _TEXT_FIELDS.items():
            value = scene.get(source)
            limit = StoryboardScene.model_fields[target].metadata
            max_length = next(
                (m.max_length for m in limit if getattr(m, "max_length", None) is not None), None
            )
            if value is not None and max_length is not None and len(value) > max_length:
                raise StoryboardSchemaViolationError(
                    f"{label}: {source} is {len(value)} chars (max {max_length})"
                )
            texts[target] = value
        if not (texts["visual_description"] or "").strip():
            raise StoryboardSchemaViolationError(f"{label}: description is empty")

        start_ms = round(float(scene["start_seconds"]) * 1000)
        end_ms = round(float(scene["end_seconds"]) * 1000)
        return StoryboardSceneDraft(
            script_scene_id=section_id,
            start_ms=start_ms,
            duration_ms=end_ms - start_ms,
            visual_kind=kind,
            visual_description=texts["visual_description"] or "",
            framing=texts["framing"],
            camera_movement=texts["camera_movement"],
            transition_in=texts["transition_in"],
        )


__all__ = [
    "GENERATOR_KIND",
    "PROVIDER_ID",
    "OpenMontageGuidedStoryboardGenerator",
    "OpenMontageSpec",
    "compute_generation_spec_id",
    "load_openmontage_spec",
    "to_openmontage_script",
]
