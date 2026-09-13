"""本物の Codex CLI + 固定 commit の OpenMontage 仕様で storyboard を1回だけ生成する。

実行するには ``AVP_LIVE_CODEX=1 pytest -m live tests/live`` と明示する必要がある。
OpenMontage の checkout は ``OPENMONTAGE_REPO_PATH``（無ければ設定値）から ``git show`` で読む。
"""

from __future__ import annotations

import uuid
from pathlib import Path

from contracts.artifacts import (
    ScriptArtifact,
    build_storyboard_artifact,
    parse_storyboard_artifact,
)
from domain.storyboard.coverage import check_storyboard_covers_script
from domain.storyboard.normalize import assign_scene_identity
from domain.storyboard.ports import StoryboardRequest
from infrastructure.config import Settings
from infrastructure.providers.codex_cli import CodexCliStoryGenerator, resolve_codex_binary
from infrastructure.providers.openmontage_storyboard import (
    OpenMontageGuidedStoryboardGenerator,
    load_openmontage_spec,
)
from infrastructure.providers.process import SubprocessRunner
from infrastructure.workdir import WorkDirectory

DEFAULT_REPO = "/home/yoshiki/projects/ai-toolbox/repos/OpenMontage"


def _script(episode_id: str) -> ScriptArtifact:
    return ScriptArtifact.model_validate(
        {
            "episode_id": episode_id,
            "type": "script",
            "schema_version": "1.0",
            "language": "ja",
            "title": "富士山はなぜ美しい形なのか",
            "hook": "左右対称の秘密",
            "scenes": [
                {
                    "id": "s1",
                    "narration": "富士山がほぼ左右対称に見えるのには理由があります。",
                    "visual": "朝焼けの富士山の全景",
                    "duration_ms": 6000,
                },
                {
                    "id": "s2",
                    "narration": "何度も噴火を重ね、溶岩と火山灰が均等に積もった成層火山だからです。",  # noqa: E501
                    "visual": "地層が積み重なる断面図",
                    "duration_ms": 8000,
                },
                {
                    "id": "s3",
                    "narration": "その積み重ねが、あの美しい稜線を作ったとされています。",
                    "visual": "稜線をなぞる空撮",
                    "duration_ms": 6000,
                },
            ],
            "metadata": {"topic": "富士山", "generator": "live", "generator_model": "live"},
        }
    )


async def test_openmontage_guided_storyboard_end_to_end(tmp_path: Path) -> None:
    settings = Settings()
    runner = SubprocessRunner()
    (tmp_path / "codex").mkdir()
    spec = await load_openmontage_spec(
        repo_path=settings.openmontage_repo_path or DEFAULT_REPO,
        commit=settings.openmontage_commit,
        runner=runner,
    )
    generator = OpenMontageGuidedStoryboardGenerator(
        llm=CodexCliStoryGenerator(
            binary=resolve_codex_binary(settings.codex_binary),
            model=settings.codex_model,
            runner=runner,
            workspace=tmp_path / "codex",
        ),
        spec=spec,
        workdir=WorkDirectory(tmp_path / "work"),
        model_label=settings.codex_model or "default",
    )
    episode_id, job_id = str(uuid.uuid4()), str(uuid.uuid4())
    script = _script(episode_id)
    raw = await generator.generate(
        StoryboardRequest(
            episode_id=episode_id,
            job_id=job_id,
            script=script,
            timeout_seconds=settings.storyboard_timeout_seconds,
        )
    )
    assert raw.text.strip()
    drafts = generator.interpret(raw.text, script)
    payload = build_storyboard_artifact(
        episode_id=episode_id,
        source_script={
            "artifact_id": str(uuid.uuid4()),
            "sha256": "0" * 64,
            "schema_version": "1.0",
        },
        scenes=assign_scene_identity(drafts),
        total_duration_ms=script.total_duration_ms,
        metadata={
            "generator": generator.generator_id,
            "generator_model": raw.model or "unknown",
            "generation_spec_id": generator.generation_spec_id,
        },
    )
    check_storyboard_covers_script(parse_storyboard_artifact(payload), script)
    assert not (tmp_path / "work" / "episodes" / episode_id / job_id).exists()
