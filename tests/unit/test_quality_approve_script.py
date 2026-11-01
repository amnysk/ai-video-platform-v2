# ruff: noqa: F811
"""``scripts/quality-approve.py``: the owner's host CLI that releases a REVIEW (ADR-0081 s9).

Why each test exists: docs/testing/quality-approve-rationale.md. Real sqlite + in-memory store;
a fake workflow starter. No Temporal, no YouTube, no fal.
"""

from __future__ import annotations

import json
import pathlib
from typing import Any

import pytest
from sqlalchemy import func, select

from contracts.quality import QUALITY_GATE_ENFORCE_ENV, QualityReason, QualityVerdict
from contracts.states import ArtifactType
from domain.quality.deterministic import QualityAssessment
from domain.quality.upload_decision import UploadDecision
from infrastructure.config import Settings
from infrastructure.db.models import QualityManualApprovalRow
from infrastructure.db.repositories import ArtifactMetadataRepository, EpisodeRepository
from infrastructure.quality.service import GateOutcome
from tests.support.script_loader import load_script_module
from tests.unit.test_quality_gate_persisted_wiring import (  # noqa: F401  (fixtures)
    Starter,
    factory,
    world,
)

SCRIPT = pathlib.Path(__file__).resolve().parents[2] / "scripts" / "quality-approve.py"
REF = "OWNER-20261102-abc123"
AGENT_REF = "AGENT-20261102-agent1:OWNER-20261102-abc123"


@pytest.fixture
def cli():
    return load_script_module("quality_approve_script", SCRIPT)


def _settings() -> Settings:
    return Settings(_env_file=None, **{QUALITY_GATE_ENFORCE_ENV: True})  # type: ignore[arg-type]


async def _run(cli, world, args: list[str], *, service=None, starter=None):
    starter = starter or Starter()
    code, out = await cli.run(
        args,
        session_factory=world.factory,
        service=service or world.service(),
        starter=starter,
        settings=_settings(),
    )
    json.dumps(out, default=str)  # JSON serialisable
    return code, out, starter


async def _approvals(world: Any) -> int:
    async with world.factory() as s:
        return (
            await s.execute(select(func.count()).select_from(QualityManualApprovalRow))
        ).scalar_one()


class BlockService:
    """BLOCK を返す。承認を書こうとしたら失敗として数える。"""

    def __init__(self, real) -> None:
        self.writes = 0
        self._real = real

    async def evaluate(self, episode_id: str, *, manual: bool) -> GateOutcome:
        a = QualityAssessment(
            UploadDecision(False, QualityReason.METADATA_LINT_FAILED), QualityVerdict.BLOCK
        )
        return GateOutcome(a, "a" * 64, None)

    async def record_manual_approval(self, *a: Any, **k: Any) -> str:
        self.writes += 1
        raise AssertionError("must not be written")


def _failing_write(world):
    service = world.service()

    async def boom(*a: Any, **k: Any) -> str:
        raise RuntimeError("db down")

    service.record_manual_approval = boom  # type: ignore[method-assign]
    return service


@pytest.mark.parametrize(
    "ref", [AGENT_REF, "", "owner-20261102-abc123", "OWNER-20261399-abc123", " " + REF, "x"]
)
async def test_only_the_owners_own_ref_is_accepted(cli, world, ref) -> None:
    code, out, starter = await _run(
        cli, world, [world.episode_id, "--owner-approval", ref, "--execute"]
    )
    assert code == cli.EXIT_REFUSED and "error" in out
    assert starter.started == [] and await _approvals(world) == 0


async def test_dry_run_prints_the_outcome_and_writes_and_starts_nothing(cli, world) -> None:
    code, out, starter = await _run(cli, world, [world.episode_id, "--owner-approval", REF])
    assert code == cli.EXIT_OK
    assert out["executed"] is False and out["review_approved"] is True
    assert out["reason_code"]
    assert starter.started == [] and await _approvals(world) == 0


async def test_block_writes_nothing_and_starts_nothing(cli, world) -> None:
    service = BlockService(world.service())
    code, out, starter = await _run(
        cli, world, [world.episode_id, "--owner-approval", REF, "--execute"], service=service
    )
    assert code == cli.EXIT_REFUSED
    assert starter.started == [] and service.writes == 0 and await _approvals(world) == 0


async def test_a_gate_that_raises_writes_nothing_and_starts_nothing(cli, world) -> None:
    class Boom:
        async def evaluate(self, *a: Any, **k: Any) -> GateOutcome:
            raise RuntimeError("x")

    code, _out, starter = await _run(
        cli, world, [world.episode_id, "--owner-approval", REF, "--execute"], service=Boom()
    )
    assert code == cli.EXIT_REFUSED and starter.started == [] and await _approvals(world) == 0


async def test_a_failed_approval_write_starts_no_workflow(cli, world) -> None:
    code, _out, starter = await _run(
        cli,
        world,
        [world.episode_id, "--owner-approval", REF, "--execute"],
        service=_failing_write(world),
    )
    assert code == cli.EXIT_REFUSED
    assert starter.started == [] and await _approvals(world) == 0


async def test_success_records_one_owner_approval_and_starts_the_api_workflow(cli, world) -> None:
    from apps.api.workflow_starter import upload_workflow_id

    async with world.factory() as s:
        await EpisodeRepository(s).set_workflow_id(
            world.episode_id, f"{upload_workflow_id(world.episode_id)}:run-1"
        )
        await s.commit()
    code, out, starter = await _run(
        cli, world, [world.episode_id, "--owner-approval", REF, "--execute"]
    )
    assert code == cli.EXIT_OK, out
    assert out["executed"] is True and out["workflow_id"] == upload_workflow_id(world.episode_id)
    assert starter.started == [world.episode_id]
    async with world.factory() as s:
        (row,) = (await s.execute(select(QualityManualApprovalRow))).scalars().all()
    assert row.approver_kind == "owner" and REF in row.approver_id


async def test_an_existing_upload_receipt_is_refused(cli, world, monkeypatch) -> None:
    async def has_receipt(self, episode_id, artifact_type):
        return ("s" * 64, "i" * 64) if artifact_type is ArtifactType.UPLOAD_RECEIPT else None

    monkeypatch.setattr(ArtifactMetadataRepository, "find_current_hashes", has_receipt)
    code, out, starter = await _run(
        cli, world, [world.episode_id, "--owner-approval", REF, "--execute"]
    )
    assert code == cli.EXIT_REFUSED and "receipt" in out["error"]
    assert starter.started == [] and await _approvals(world) == 0


async def test_without_a_service_the_flag_is_off_and_nothing_happens(cli, world) -> None:
    code, out = await cli.run(
        [world.episode_id, "--owner-approval", REF, "--execute"],
        session_factory=world.factory,
        service=None,
        starter=Starter(),
        settings=_settings(),
    )
    assert code == cli.EXIT_REFUSED and "error" in out


async def test_a_non_uuid_episode_id_is_refused(cli, world) -> None:
    code, _out, starter = await _run(cli, world, ["nope", "--owner-approval", REF, "--execute"])
    assert code == cli.EXIT_REFUSED and starter.started == []
