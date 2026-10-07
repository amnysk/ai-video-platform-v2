"""ReviewPacket / ChangeProposalFile の契約（JP-MAS-001 §8、``docs/domain/review-packet.md``）。"""

from __future__ import annotations

from pathlib import Path

import pytest
from pydantic import ValidationError

from contracts.intervention import ProposeChangePayload
from contracts.review_packet import ChangeProposalFile, render_json_schema

ROOT = Path(__file__).resolve().parents[2]
SCHEMA_FILE = ROOT / "docs" / "domain" / "review-packet.schema.json"
BASE = "11111111-1111-1111-1111-111111111111"
M1 = "22222222-2222-2222-2222-222222222222"


def _proposal(manifest: str | None = M1) -> dict:
    return {
        "change": {
            "kind": "script.edit_scenes",
            "base_revision_id": BASE,
            "edits": [{"segment_id": "s1", "narration": "新しい一文"}],
        },
        "rationale": "読みやすく",
        "base_manifest_id": manifest,
    }


def test_the_published_json_schema_matches_the_models() -> None:
    """書き出した schema が古いまま残らない（再生成: ``python -m contracts.review_packet``）。"""
    assert SCHEMA_FILE.read_text(encoding="utf-8") == render_json_schema()


def test_each_proposal_is_exactly_a_propose_change_payload() -> None:
    """API は各 proposal をそのまま ``ProposeChangePayload`` として検証する（担当C の取り込み）。"""
    parsed = ChangeProposalFile.model_validate(
        {
            "episode_id": "e",
            "base_manifest_id": M1,
            "proposals": [_proposal()],
            "suggested_command_ids": ["proposal-0001"],
        }
    )
    for proposal in parsed.proposals:
        again = ProposeChangePayload.model_validate(proposal.model_dump(mode="json"))
        assert again.change.base_revision_id == BASE


def test_a_proposal_based_on_another_manifest_is_rejected() -> None:
    with pytest.raises(ValidationError):
        ChangeProposalFile.model_validate(
            {
                "base_manifest_id": M1,
                "proposals": [_proposal("33333333-3333-3333-3333-333333333333")],
            }
        )


@pytest.mark.parametrize(
    "body",
    [
        {"proposals": []},
        {"proposals": [_proposal()], "suggested_command_ids": ["only-one-1", "two-two-2"]},
        {"proposals": [_proposal()], "suggested_command_ids": ["short"]},
        {"proposals": [_proposal()], "budget_jpy": 999999},
    ],
    ids=["empty", "misaligned-ids", "short-id", "unknown-field"],
)
def test_malformed_files_are_rejected(body: dict) -> None:
    with pytest.raises(ValidationError):
        ChangeProposalFile.model_validate(body)
