"""OpenMontage 誘導 storyboard アダプタの単体テスト。本物の codex / OpenMontage は呼ばない。

常時実行のテストは**このファイルで書き下ろした最小スキーマ**を使う（OpenMontage の blob を
リポジトリに複製しない。AGPLv3）。固定 commit の本物の blob との突き合わせは、
ローカルに checkout がある場合だけ ``git show`` で読んで行う（無ければ skip）。
"""

from __future__ import annotations

import builtins
import json
import logging
import os
import pathlib
import shutil
import subprocess
import uuid
from collections.abc import Mapping, Sequence
from typing import Any

import pytest

from contracts.artifacts import ScriptArtifact, StoryboardVisualKind, build_storyboard_artifact
from domain.errors import (
    GenerationSpecUnavailableError,
    ProviderTimeoutError,
    StoryboardInputInvalidError,
    StoryboardOutputUnparseableError,
    StoryboardSchemaViolationError,
    WorkspaceUnavailableError,
)
from domain.storyboard.coverage import check_storyboard_covers_script
from domain.storyboard.normalize import assign_scene_identity
from domain.storyboard.ports import StoryboardGenerator, StoryboardRequest
from infrastructure.providers.openmontage_storyboard import (
    SCENE_DIRECTOR_PATH,
    SCENE_PLAN_SCHEMA_PATH,
    SCRIPT_SCHEMA_PATH,
    OpenMontageGuidedStoryboardGenerator,
    OpenMontageSpec,
    compute_generation_spec_id,
    load_openmontage_spec,
    to_openmontage_script,
)
from infrastructure.providers.process import ProcessResult, ProcessTimeout
from infrastructure.workdir import WorkDirectory
from prompts import (
    PROMPT_TEMPLATE_ID,
    STORYBOARD_PROMPT_TEMPLATE_ID,
    STORYBOARD_PROMPT_TEMPLATE_VERSION,
    load_prompt_template,
)
from tests.support.fakes import FakeStoryGenerator

PINNED_COMMIT = "2fa571e39ad0632148dad77c7a2134f7e6fe0797"
EPISODE_ID = str(uuid.UUID(int=1))
JOB_ID = str(uuid.UUID(int=2))

# --- 書き下ろしの最小スキーマ（OpenMontage からの複製ではない） ----------------

SCENE_PLAN_SCHEMA: dict[str, Any] = {
    "$schema": "https://json-schema.org/draft/2020-12/schema",
    "type": "object",
    "required": ["version", "scenes"],
    "additionalProperties": False,
    "properties": {
        "version": {"const": "1.0"},
        "scenes": {
            "type": "array",
            "minItems": 1,
            "items": {
                "type": "object",
                "required": ["id", "type", "description", "start_seconds", "end_seconds"],
                "additionalProperties": False,
                "properties": {
                    "id": {"type": "string"},
                    "type": {
                        "enum": [
                            "talking_head",
                            "broll",
                            "animation",
                            "character_scene",
                            "diagram",
                            "text_card",
                            "transition",
                            "generated",
                            "screen_recording",
                        ]
                    },
                    "description": {"type": "string"},
                    "start_seconds": {"type": "number", "minimum": 0},
                    "end_seconds": {"type": "number", "minimum": 0},
                    "script_section_id": {"type": "string"},
                    "framing": {"type": "string"},
                    "movement": {"type": "string"},
                    "transition_in": {"type": "string"},
                },
            },
        },
    },
}

SCRIPT_SCHEMA: dict[str, Any] = {
    "type": "object",
    "required": ["version", "title", "total_duration_seconds", "sections"],
    "additionalProperties": False,
    "properties": {
        "version": {"const": "1.0"},
        "title": {"type": "string"},
        "total_duration_seconds": {"type": "number", "minimum": 1},
        "metadata": {"type": "object"},
        "sections": {
            "type": "array",
            "minItems": 1,
            "items": {
                "type": "object",
                "required": ["id", "text", "start_seconds", "end_seconds"],
                "additionalProperties": False,
                "properties": {
                    "id": {"type": "string"},
                    "text": {"type": "string"},
                    "start_seconds": {"type": "number", "minimum": 0},
                    "end_seconds": {"type": "number", "minimum": 0},
                },
            },
        },
    },
}

DIRECTOR_MD = "# Scene Director (test)\nUse web search. Save a checkpoint. Ask for approval.\n"


def _spec(**overrides: Any) -> OpenMontageSpec:
    values: dict[str, Any] = {
        "commit": PINNED_COMMIT,
        "scene_director_md": DIRECTOR_MD,
        "scene_plan_schema": SCENE_PLAN_SCHEMA,
        "script_schema": SCRIPT_SCHEMA,
        "generation_spec_id": "openmontage@2fa571e39ad0/sha256:0000000000000000",
    }
    values.update(overrides)
    return OpenMontageSpec(**values)


def _script() -> ScriptArtifact:
    return ScriptArtifact.model_validate(
        {
            "episode_id": EPISODE_ID,
            "type": "script",
            "schema_version": "1.0",
            "language": "ja",
            "title": "応仁の乱",
            "hook": "11年続いた戦乱",
            "scenes": [
                {"id": "s1", "narration": "始まり", "visual": "京都の町並み", "duration_ms": 5000},
                {"id": "s2", "narration": "展開", "visual": "対立する武将", "duration_ms": 5500},
                {"id": "s3", "narration": "結末", "visual": "焼け跡", "duration_ms": 4500},
            ],
            "metadata": {"topic": "応仁の乱", "generator": "codex", "generator_model": "m"},
        }
    )


def _scene(section: str, start: float, end: float, **extra: Any) -> dict[str, Any]:
    return {
        "id": f"scene-{section}",
        "type": "diagram",
        "description": f"{section} の図解",
        "start_seconds": start,
        "end_seconds": end,
        "script_section_id": section,
        **extra,
    }


def _plan(*scenes: dict[str, Any]) -> str:
    if not scenes:
        scenes = (
            _scene("s1", 0, 5, framing="wide", movement="slow pan", transition_in="fade"),
            _scene("s2", 5, 10.5, type="character_scene"),
            _scene("s3", 10.5, 15, type="broll"),
        )
    return json.dumps({"version": "1.0", "scenes": list(scenes)}, ensure_ascii=False)


def _generator(tmp_path: pathlib.Path, llm: Any = None, **kwargs: Any):
    workdir = kwargs.pop("workdir", None) or WorkDirectory(tmp_path / "work")
    return OpenMontageGuidedStoryboardGenerator(
        llm=llm or FakeStoryGenerator(output=_plan()),
        spec=kwargs.pop("spec", None) or _spec(),
        workdir=workdir,
        model_label="gpt-test",
    )


def _request() -> StoryboardRequest:
    return StoryboardRequest(
        episode_id=EPISODE_ID, job_id=JOB_ID, script=_script(), timeout_seconds=77
    )


# --- spec の読み込み ------------------------------------------------------


class ScriptedGitRunner:
    """``git`` の引数ごとに応答を返す ProcessRunner のフェイク。"""

    def __init__(self, blobs: Mapping[str, str], *, overrides: Mapping[str, Any] = {}) -> None:
        self.blobs = dict(blobs)
        self.overrides = dict(overrides)
        self.calls: list[list[str]] = []

    async def run(
        self,
        argv: Sequence[str],
        *,
        stdin: str,
        env: Mapping[str, str],
        timeout_seconds: int,
        cwd: str | None = None,
    ) -> ProcessResult:
        argv = list(argv)
        self.calls.append(argv)
        key = argv[-1]
        if key in self.overrides:
            value = self.overrides[key]
            if isinstance(value, BaseException):
                raise value
            return value
        if argv[3] == "rev-parse":
            return ProcessResult(0, PINNED_COMMIT + "\n", "")
        path = key.split(":", 1)[1]
        if path not in self.blobs:
            return ProcessResult(128, "", f"fatal: path '{path}' does not exist")
        return ProcessResult(0, self.blobs[path], "")


def _blobs() -> dict[str, str]:
    return {
        SCENE_DIRECTOR_PATH: DIRECTOR_MD,
        SCENE_PLAN_SCHEMA_PATH: json.dumps(SCENE_PLAN_SCHEMA),
        SCRIPT_SCHEMA_PATH: json.dumps(SCRIPT_SCHEMA),
    }


async def test_load_spec_reads_three_blobs_with_git_show_argv(tmp_path) -> None:
    runner = ScriptedGitRunner(_blobs())
    spec = await load_openmontage_spec(repo_path=tmp_path, commit="2fa571e", runner=runner)
    assert spec.commit == PINNED_COMMIT
    assert spec.scene_director_md == DIRECTOR_MD
    assert spec.scene_plan_schema == SCENE_PLAN_SCHEMA
    assert spec.script_schema == SCRIPT_SCHEMA
    assert spec.generation_spec_id.startswith("openmontage@2fa571e39ad0/sha256:")
    shows = [c for c in runner.calls if c[3] == "show"]
    assert {c[-1] for c in shows} == {
        f"{PINNED_COMMIT}:{p}"
        for p in (SCENE_DIRECTOR_PATH, SCENE_PLAN_SCHEMA_PATH, SCRIPT_SCHEMA_PATH)
    }
    for call in runner.calls:
        assert call[:3] == ["git", "-C", str(tmp_path)]


async def test_generation_spec_id_is_stable_and_changes_with_any_blob(tmp_path) -> None:
    first = await load_openmontage_spec(
        repo_path=tmp_path, commit=PINNED_COMMIT, runner=ScriptedGitRunner(_blobs())
    )
    again = await load_openmontage_spec(
        repo_path=tmp_path, commit=PINNED_COMMIT, runner=ScriptedGitRunner(_blobs())
    )
    assert first.generation_spec_id == again.generation_spec_id
    for path in _blobs():
        changed = _blobs()
        changed[path] = changed[path] + " "
        other = await load_openmontage_spec(
            repo_path=tmp_path, commit=PINNED_COMMIT, runner=ScriptedGitRunner(changed)
        )
        assert other.generation_spec_id != first.generation_spec_id, path


def test_generation_spec_id_depends_on_commit() -> None:
    assert compute_generation_spec_id("a" * 40, _blobs()) != compute_generation_spec_id(
        "b" * 40, _blobs()
    )


@pytest.mark.parametrize(
    "overrides",
    [
        {f"{PINNED_COMMIT}^{{commit}}": ProcessResult(128, "", "fatal: not a git repository")},
        {f"{PINNED_COMMIT}^{{commit}}": ProcessResult(128, "", "fatal: Needed a single revision")},
        {f"{PINNED_COMMIT}^{{commit}}": FileNotFoundError(2, "git")},
        {f"{PINNED_COMMIT}^{{commit}}": ProcessTimeout("timed out")},
        {f"{PINNED_COMMIT}:{SCRIPT_SCHEMA_PATH}": ProcessResult(0, "{not json", "")},
        {f"{PINNED_COMMIT}:{SCENE_PLAN_SCHEMA_PATH}": ProcessResult(0, "[]", "")},
        {f"{PINNED_COMMIT}:{SCENE_PLAN_SCHEMA_PATH}": ProcessResult(0, '{"type": 5}', "")},
    ],
    ids=[
        "missing-repo",
        "bad-commit",
        "no-git",
        "timeout",
        "invalid-json",
        "not-object",
        "bad-schema",
    ],
)
async def test_load_spec_failures_become_generation_spec_unavailable(tmp_path, overrides) -> None:
    runner = ScriptedGitRunner(_blobs(), overrides=overrides)
    with pytest.raises(GenerationSpecUnavailableError):
        await load_openmontage_spec(repo_path=tmp_path, commit=PINNED_COMMIT, runner=runner)


async def test_load_spec_missing_blob_is_unavailable(tmp_path) -> None:
    blobs = _blobs()
    del blobs[SCENE_DIRECTOR_PATH]
    with pytest.raises(GenerationSpecUnavailableError):
        await load_openmontage_spec(
            repo_path=tmp_path, commit=PINNED_COMMIT, runner=ScriptedGitRunner(blobs)
        )


async def test_load_spec_rejects_option_like_commit(tmp_path) -> None:
    runner = ScriptedGitRunner(_blobs())
    with pytest.raises(GenerationSpecUnavailableError):
        await load_openmontage_spec(repo_path=tmp_path, commit="--output=x", runner=runner)
    assert runner.calls == []


# --- 同一性 ---------------------------------------------------------------


def test_generator_identity_and_protocol(tmp_path) -> None:
    generator = _generator(tmp_path)
    assert isinstance(generator, StoryboardGenerator)
    assert generator.generator_id == "openmontage-guided:codex:gpt-test"
    assert generator.generation_spec_id == _spec().generation_spec_id


# --- 入力変換 -------------------------------------------------------------


def test_script_conversion_shape() -> None:
    converted = to_openmontage_script(_script())
    assert converted["version"] == "1.0"
    assert converted["total_duration_seconds"] == 15
    assert [
        (s["id"], s["text"], s["start_seconds"], s["end_seconds"]) for s in converted["sections"]
    ] == [("s1", "始まり", 0, 5), ("s2", "展開", 5, 10.5), ("s3", "結末", 10.5, 15)]


async def test_conversion_failing_the_script_schema_is_input_invalid(tmp_path) -> None:
    strict = json.loads(json.dumps(SCRIPT_SCHEMA))
    del strict["properties"]["metadata"]
    llm = FakeStoryGenerator(output=_plan())
    with pytest.raises(StoryboardInputInvalidError):
        await _generator(tmp_path, llm, spec=_spec(script_schema=strict)).generate(_request())
    assert llm.calls == 0


# --- generate -------------------------------------------------------------


async def test_generate_writes_audit_files_prompts_and_cleans_up(tmp_path, monkeypatch) -> None:
    seen: dict[str, Any] = {}
    workdir = WorkDirectory(tmp_path / "work")

    def _on_call(request) -> None:
        job = workdir.create(EPISODE_ID, JOB_ID)
        seen["script"] = json.loads((job.input / "script.openmontage.json").read_text("utf-8"))
        seen["md"] = (job.openmontage / "scene-director.md").read_text("utf-8")
        seen["schema"] = json.loads((job.openmontage / "scene_plan.schema.json").read_text("utf-8"))
        seen["base"] = job.base

    llm = FakeStoryGenerator(output="RAW TEXT", on_call=_on_call, model="m-1")
    raw_path: dict[str, pathlib.Path] = {}
    original_write = pathlib.Path.write_text

    def _spy(self, data, *args, **kwargs):
        if self.name == "scene_plan.raw.txt":
            raw_path["path"] = self
            raw_path["data"] = data
        return original_write(self, data, *args, **kwargs)

    monkeypatch.setattr(pathlib.Path, "write_text", _spy)
    result = await _generator(tmp_path, llm, workdir=workdir).generate(_request())

    assert (result.text, result.provider_id, result.model) == ("RAW TEXT", "codex", "m-1")
    assert seen["script"] == to_openmontage_script(_script())
    assert seen["md"] == DIRECTOR_MD
    assert seen["schema"] == SCENE_PLAN_SCHEMA
    assert raw_path["data"] == "RAW TEXT"
    assert raw_path["path"].parent.name == "output"
    assert not seen["base"].exists(), "作業領域は成功時にも消す"

    request = llm.requests[0]
    assert request.output_schema is None
    assert request.timeout_seconds == 77
    assert request.episode_id == EPISODE_ID
    assert DIRECTOR_MD.strip() in request.prompt
    assert '"script_section_id"' in request.prompt
    assert "始まり" in request.prompt
    assert "{{" not in request.prompt.replace(DIRECTOR_MD, "")


async def test_provider_errors_propagate_and_workdir_is_cleaned(tmp_path) -> None:
    workdir = WorkDirectory(tmp_path / "work")
    llm = FakeStoryGenerator(fail_times=1, error=ProviderTimeoutError("slow"))
    with pytest.raises(ProviderTimeoutError):
        await _generator(tmp_path, llm, workdir=workdir).generate(_request())
    assert llm.calls == 1
    assert not (tmp_path / "work" / "episodes" / EPISODE_ID / JOB_ID).exists()


class _BrokenCleanup(WorkDirectory):
    def cleanup(self, episode_id: str, job_id: str) -> bool:
        raise WorkspaceUnavailableError("cannot remove")


async def test_cleanup_failure_does_not_mask_the_result(tmp_path, caplog) -> None:
    workdir = _BrokenCleanup(tmp_path / "work")
    with caplog.at_level(logging.WARNING):
        result = await _generator(tmp_path, workdir=workdir).generate(_request())
    assert result.text == _plan()
    assert any("clean up" in r.getMessage() for r in caplog.records)


async def test_cleanup_failure_does_not_mask_the_provider_error(tmp_path, caplog) -> None:
    workdir = _BrokenCleanup(tmp_path / "work")
    llm = FakeStoryGenerator(fail_times=1, error=ProviderTimeoutError("slow"))
    with caplog.at_level(logging.WARNING), pytest.raises(ProviderTimeoutError):
        await _generator(tmp_path, llm, workdir=workdir).generate(_request())
    assert any(r.levelno == logging.WARNING for r in caplog.records)


# --- interpret ------------------------------------------------------------


def test_interpret_maps_fields_and_normalizes(tmp_path) -> None:
    drafts = _generator(tmp_path).interpret(_plan(), _script())
    assert [(d.script_scene_id, d.start_ms, d.duration_ms) for d in drafts] == [
        ("s1", 0, 5000),
        ("s2", 5000, 5500),
        ("s3", 10500, 4500),
    ]
    first = drafts[0]
    assert (first.framing, first.camera_movement, first.transition_in) == (
        "wide",
        "slow pan",
        "fade",
    )
    assert first.visual_kind is StoryboardVisualKind.DIAGRAM
    assert drafts[1].visual_kind is StoryboardVisualKind.CHARACTER
    assert drafts[2].framing is None


def test_interpreted_drafts_build_a_valid_artifact_that_covers_the_script(tmp_path) -> None:
    script = _script()
    drafts = _generator(tmp_path).interpret(_plan(), script)
    artifact = build_storyboard_artifact(
        episode_id=EPISODE_ID,
        source_script={
            "artifact_id": str(uuid.uuid4()),
            "sha256": "0" * 64,
            "schema_version": "1.0",
        },
        scenes=assign_scene_identity(drafts),
        total_duration_ms=script.total_duration_ms,
        metadata={"generator": "g", "generator_model": "m", "generation_spec_id": "x"},
    )
    from contracts.artifacts import parse_storyboard_artifact

    check_storyboard_covers_script(parse_storyboard_artifact(artifact), script)


def test_interpret_accepts_fenced_json(tmp_path) -> None:
    assert len(_generator(tmp_path).interpret(f"```json\n{_plan()}\n```", _script())) == 3


@pytest.mark.parametrize("raw", ["", "no json here", '{"version": "1.0", "scenes": [', "}{"])
def test_garbage_is_unparseable(tmp_path, raw: str) -> None:
    with pytest.raises(StoryboardOutputUnparseableError):
        _generator(tmp_path).interpret(raw, _script())


@pytest.mark.parametrize(
    "plan",
    [
        _plan(_scene("s1", 0, 5, bogus="x"), _scene("s2", 5, 10.5), _scene("s3", 10.5, 15)),
        _plan(_scene("s1", 0, 5, type="hologram"), _scene("s2", 5, 10.5), _scene("s3", 10.5, 15)),
        json.dumps({"version": "1.0", "scenes": [{"id": "x", "type": "broll"}]}),
        json.dumps({"version": "2.0", "scenes": json.loads(_plan())["scenes"]}),
    ],
    ids=["extra-field", "bad-type-enum", "missing-required", "bad-version"],
)
def test_schema_violations(tmp_path, plan: str) -> None:
    with pytest.raises(StoryboardSchemaViolationError) as excinfo:
        _generator(tmp_path).interpret(plan, _script())
    assert "schema violation" in str(excinfo.value)


def test_schema_violation_message_is_bounded(tmp_path) -> None:
    scenes = [{"id": str(i), "bogus": "y" * 1000} for i in range(30)]
    with pytest.raises(StoryboardSchemaViolationError) as excinfo:
        _generator(tmp_path).interpret(json.dumps({"version": "1.0", "scenes": scenes}), _script())
    assert len(str(excinfo.value)) < 1500
    assert "more" in str(excinfo.value)


@pytest.mark.parametrize("section", [None, "s9"], ids=["missing", "unknown"])
def test_missing_or_unknown_section_id_is_a_violation(tmp_path, section) -> None:
    bad = _scene("s3", 10.5, 15)
    if section is None:
        del bad["script_section_id"]
    else:
        bad["script_section_id"] = section
    with pytest.raises(StoryboardSchemaViolationError, match="script_section_id"):
        _generator(tmp_path).interpret(
            _plan(_scene("s1", 0, 5), _scene("s2", 5, 10.5), bad), _script()
        )


@pytest.mark.parametrize(
    ("field", "size"),
    [("description", 601), ("framing", 121), ("movement", 121), ("transition_in", 61)],
)
def test_overlong_strings_are_violations_not_truncated(tmp_path, field: str, size: int) -> None:
    first = _scene("s1", 0, 5, **{field: "あ" * size})
    with pytest.raises(StoryboardSchemaViolationError, match=field):
        _generator(tmp_path).interpret(
            _plan(first, _scene("s2", 5, 10.5), _scene("s3", 10.5, 15)), _script()
        )


def test_strings_at_the_limit_are_accepted(tmp_path) -> None:
    first = _scene("s1", 0, 5, description="a" * 600, framing="b" * 120, transition_in="c" * 60)
    drafts = _generator(tmp_path).interpret(
        _plan(first, _scene("s2", 5, 10.5), _scene("s3", 10.5, 15)), _script()
    )
    assert len(drafts[0].visual_description) == 600


def test_gap_beyond_snap_tolerance_is_a_violation(tmp_path) -> None:
    with pytest.raises(StoryboardSchemaViolationError):
        _generator(tmp_path).interpret(
            _plan(_scene("s1", 0, 5), _scene("s2", 6, 10.5), _scene("s3", 10.5, 15)), _script()
        )


def test_small_drift_is_snapped(tmp_path) -> None:
    drafts = _generator(tmp_path).interpret(
        _plan(_scene("s1", 0.2, 5.3), _scene("s2", 5, 10.4), _scene("s3", 10.6, 15.7)), _script()
    )
    assert drafts[0].start_ms == 0
    assert drafts[1].start_ms == 5300
    assert drafts[2].start_ms == 10400
    assert drafts[-1].start_ms + drafts[-1].duration_ms == 15000


def test_seconds_are_rounded_to_milliseconds(tmp_path) -> None:
    drafts = _generator(tmp_path).interpret(
        _plan(_scene("s1", 0, 5.0004), _scene("s2", 5.0004, 10.5), _scene("s3", 10.5, 15)),
        _script(),
    )
    assert drafts[1].start_ms == 5000


def test_interpret_never_touches_the_filesystem(tmp_path, monkeypatch) -> None:
    generator = _generator(tmp_path)

    def _forbidden(*args, **kwargs):
        raise AssertionError("interpret must not do I/O")

    monkeypatch.setattr(builtins, "open", _forbidden)
    monkeypatch.setattr(pathlib.Path, "write_text", _forbidden)
    monkeypatch.setattr(pathlib.Path, "read_text", _forbidden)
    monkeypatch.setattr(pathlib.Path, "mkdir", _forbidden)
    monkeypatch.setattr(os, "lstat", _forbidden)
    generator.interpret(_plan(), _script())
    assert not (tmp_path / "work").exists()


# --- プロンプトテンプレート ------------------------------------------------


def test_storyboard_template_constants_and_rules() -> None:
    assert STORYBOARD_PROMPT_TEMPLATE_ID == "storyboard_ja"
    assert STORYBOARD_PROMPT_TEMPLATE_VERSION == "1"
    assert PROMPT_TEMPLATE_ID == "script_ja"
    template = load_prompt_template(STORYBOARD_PROMPT_TEMPLATE_ID)
    for token in ("script_section_id", "コードフェンス", "Web 検索", "承認", "チェックポイント"):
        assert token in template


# --- 固定 commit の本物の blob（ローカルに checkout がある場合だけ） ---------

OPENMONTAGE_REPO = os.environ.get(
    "OPENMONTAGE_REPO_PATH", "/home/yoshiki/projects/ai-toolbox/repos/OpenMontage"
)


def _pinned_blob(path: str) -> str:
    if shutil.which("git") is None or not pathlib.Path(OPENMONTAGE_REPO).is_dir():
        pytest.skip("OpenMontage checkout not available")
    proc = subprocess.run(
        ["git", "-C", OPENMONTAGE_REPO, "show", f"{PINNED_COMMIT}:{path}"],
        capture_output=True,
        text=True,
        check=False,
    )
    if proc.returncode != 0:
        pytest.skip(f"pinned commit not available: {proc.stderr.strip()}")
    return proc.stdout


@pytest.fixture(scope="module")
def pinned_spec() -> OpenMontageSpec:
    blobs = {
        p: _pinned_blob(p)
        for p in (SCENE_DIRECTOR_PATH, SCENE_PLAN_SCHEMA_PATH, SCRIPT_SCHEMA_PATH)
    }
    return OpenMontageSpec(
        commit=PINNED_COMMIT,
        scene_director_md=blobs[SCENE_DIRECTOR_PATH],
        scene_plan_schema=json.loads(blobs[SCENE_PLAN_SCHEMA_PATH]),
        script_schema=json.loads(blobs[SCRIPT_SCHEMA_PATH]),
        generation_spec_id=compute_generation_spec_id(PINNED_COMMIT, blobs),
    )


def test_conversion_is_valid_against_the_pinned_script_schema(pinned_spec) -> None:
    from jsonschema import Draft202012Validator

    Draft202012Validator(pinned_spec.script_schema).validate(to_openmontage_script(_script()))


def test_every_pinned_scene_type_is_mapped_and_mapped_fields_exist(pinned_spec) -> None:
    from infrastructure.providers.openmontage_storyboard import VISUAL_KIND_BY_SCENE_TYPE

    item = pinned_spec.scene_plan_schema["properties"]["scenes"]["items"]
    assert set(item["properties"]["type"]["enum"]) == set(VISUAL_KIND_BY_SCENE_TYPE)
    for field in ("description", "framing", "movement", "transition_in", "script_section_id"):
        assert item["properties"][field]["type"] == "string"


def test_interpret_against_the_pinned_scene_plan_schema(tmp_path, pinned_spec) -> None:
    generator = _generator(tmp_path, spec=pinned_spec)
    assert len(generator.interpret(_plan(), _script())) == 3
    with pytest.raises(StoryboardSchemaViolationError):
        generator.interpret(
            _plan(_scene("s1", 0, 5, bogus=1), _scene("s2", 5, 10.5), _scene("s3", 10.5, 15)),
            _script(),
        )


async def test_generate_with_the_pinned_spec(tmp_path, pinned_spec) -> None:
    llm = FakeStoryGenerator(output=_plan())
    result = await _generator(tmp_path, llm, spec=pinned_spec).generate(_request())
    assert result.text == _plan()
    assert "Scene Director" in llm.requests[0].prompt
