# ruff: noqa: F811 - 別ファイルの fixture（world）を使う
"""``scripts/quality_shadow.py``: 過去 Episode の書き出しに旧 gate と新 gate を当てる。

ADR-0087 §8。

合成した書き出しは、実の render で作った Episode（fake エンジン）を DB と store から書き出して作る。
理由は docs/testing/bgm-auto-quality-rationale.md。
"""

from __future__ import annotations

import hashlib
import json
import pathlib
from datetime import UTC, datetime
from typing import Any

import pytest
from sqlalchemy import select

from domain.artifact.hashing import canonical_json_bytes, sha256_hex
from infrastructure.db.models import ArtifactMetadataRow
from tests.support.script_loader import load_script_module
from tests.unit.test_quality_gate_persisted_wiring import (  # noqa: F401  (fixtures)
    World,
    factory,
    world,
)

SCRIPT = pathlib.Path(__file__).resolve().parents[2] / "scripts" / "quality_shadow.py"
NOW = datetime(2026, 10, 30, 12, 0, tzinfo=UTC)


@pytest.fixture
def cli():
    return load_script_module("quality_shadow_script", SCRIPT)


async def export_world(world: World, out: pathlib.Path, *, usage: dict[str, bool] | None = None):
    """world の Episode を、README にある配置の書き出しにする。"""
    directory = out / world.episode_id
    directory.mkdir(parents=True)
    async with world.factory() as s:
        rows = (
            (
                await s.execute(
                    select(ArtifactMetadataRow).where(
                        ArtifactMetadataRow.episode_id == _uuid(world.episode_id),
                        ArtifactMetadataRow.artifact_type.in_(["final_video", "script"]),
                    )
                )
            )
            .scalars()
            .all()
        )
    metadata: list[dict[str, Any]] = []
    for row in rows:
        metadata.append(
            {
                "id": str(row.id),
                "artifact_type": row.artifact_type,
                "schema_version": row.schema_version,
                "bucket": row.bucket,
                "object_key": row.object_key,
                "sha256": row.sha256,
                "input_hash": row.input_hash,
                "version": row.version,
                "superseded_at": row.superseded_at.isoformat() if row.superseded_at else None,
            }
        )
        name = "final_video.json" if row.artifact_type == "final_video" else "script.json"
        (directory / name).write_bytes(
            canonical_json_bytes(await world.store.get_json(row.object_key))
        )
    (directory / "artifact_metadata.json").write_text(json.dumps(metadata))
    if usage is not None:
        (directory / "usage.json").write_text(json.dumps(usage))
    return directory


def _uuid(value: str):
    import uuid

    return uuid.UUID(value)


def _tree_digest(root: pathlib.Path) -> dict[str, str]:
    return {
        str(p.relative_to(root)): hashlib.sha256(p.read_bytes()).hexdigest()
        for p in sorted(root.rglob("*"))
        if p.is_file()
    }


async def test_a_clean_export_is_allowed_by_both_gates_and_the_export_is_untouched(
    cli, world: World, tmp_path
) -> None:
    out = tmp_path / "export"
    await export_world(world, out)
    before = _tree_digest(out)
    report = await cli.evaluate_export(out, now=NOW)
    assert _tree_digest(out) == before  # 読み取り専用
    (ep,) = report["episodes"]
    assert ep["old"]["allowed"] and ep["new"]["allowed"]
    assert ep["new"]["reason"] == "upload_allowed" and ep["new"]["verdict"] == "pass"
    s = report["summary"]
    assert s["episodes_evaluated"] == 1 and s["export_errors"] == 0
    assert s["new"]["passed"] == 1 and s["new"]["upload_allowed"] == 1
    assert s["auto_upload_allowed_rate"] == {"old": 1.0, "new": 1.0}
    assert s["auto_recovered"] == 0 and s["inspection_error_rate"] == 0.0


async def test_injected_transient_faults_within_the_retry_budget_are_recovered(
    cli, world: World, tmp_path
) -> None:
    out = tmp_path / "export"
    await export_world(world, out)
    report = await cli.evaluate_export(out, now=NOW, inject_transient=2)
    (ep,) = report["episodes"]
    assert ep["new"]["allowed"] and ep["new"]["auto_recovered"]
    assert ep["new"]["recovered_faults"] == 2
    assert report["summary"]["auto_recovered"] == 1


async def test_faults_beyond_the_budget_are_inspection_unavailable_never_a_pass(
    cli, world: World, tmp_path
) -> None:
    out = tmp_path / "export"
    await export_world(world, out)
    report = await cli.evaluate_export(out, now=NOW, inject_transient=10_000)
    (ep,) = report["episodes"]
    assert not ep["new"]["allowed"]
    assert ep["new"]["reason"] == "inspection_unavailable"
    assert ep["new"]["hold_cause"] == "inspection"
    assert ep["new"]["verdict"] == "review"
    s = report["summary"]
    assert s["new"]["inspection_unavailable"] == 1 and s["inspection_error_rate"] == 1.0
    assert s["newly_allowed"] == [] and s["newly_refused"] == [ep["episode_id"]]
    assert s["auto_recovered"] == 0


async def test_a_tampered_final_video_is_refused_by_the_new_gate(
    cli, world: World, tmp_path
) -> None:
    out = tmp_path / "export"
    directory = await export_world(world, out)
    body = json.loads((directory / "final_video.json").read_text())
    body["template_version"] += 1  # artifact_metadata の sha256 と合わない
    (directory / "final_video.json").write_bytes(canonical_json_bytes(body))
    report = await cli.evaluate_export(out, now=NOW)
    (ep,) = report["episodes"]
    assert not ep["new"]["allowed"]
    assert ep["new"]["hold_cause"] == "quality"  # 検査系の故障ではなく本物の不整合
    assert report["summary"]["new"]["inspection_unavailable"] == 0


async def test_used_features_without_evidence_are_a_real_missing_check_not_a_pass(
    cli, world: World, tmp_path
) -> None:
    out = tmp_path / "export"
    await export_world(world, out, usage={"bgm": True, "map": False})
    report = await cli.evaluate_export(out, now=NOW)
    (ep,) = report["episodes"]
    assert not ep["new"]["allowed"] and ep["new"]["verdict"] == "review"
    assert ep["new"]["reason"] == "report_needs_review"


async def test_without_assumed_usage_the_declaration_is_missing_review(
    cli, world: World, tmp_path
) -> None:
    out = tmp_path / "export"
    await export_world(world, out)
    report = await cli.evaluate_export(out, now=NOW, assume_usage=False)
    (ep,) = report["episodes"]
    assert not ep["new"]["allowed"] and ep["new"]["verdict"] == "review"


async def test_a_broken_episode_is_reported_without_stopping_the_others(
    cli, world: World, tmp_path
) -> None:
    out = tmp_path / "export"
    await export_world(world, out)
    broken = out / "00000000-0000-4000-8000-000000000001"
    broken.mkdir()
    (broken / "final_video.json").write_text("{not json")
    report = await cli.evaluate_export(out, now=NOW)
    by_id = {e["episode_id"]: e for e in report["episodes"]}
    assert "export_error" in by_id[broken.name]
    assert by_id[world.episode_id]["new"]["allowed"]
    assert report["summary"]["export_errors"] == 1 and report["summary"]["episodes_evaluated"] == 1


async def test_the_command_line_prints_json(cli, world: World, tmp_path, capsys) -> None:
    out = tmp_path / "export"
    await export_world(world, out)
    code = await cli.main([str(out), "--now", NOW.isoformat(), "--inject-transient", "1"])
    assert code == 0
    printed = json.loads(capsys.readouterr().out)
    assert printed["inject_transient"] == 1
    assert printed["summary"]["auto_recovered"] == 1


def test_the_tool_never_connects_to_production() -> None:
    """インメモリ sqlite / メモリのストアだけ。実ネットワーク・本番の設定を読まない。"""
    src = SCRIPT.read_text()
    for forbidden in ("Settings(", "MinioArtifactStore", "temporalio", "asyncpg", "database_url"):
        assert forbidden not in src, forbidden
    assert "sqlite+aiosqlite://" in src and "InMemoryArtifactStore" in src
    assert sha256_hex(b"") is not None
