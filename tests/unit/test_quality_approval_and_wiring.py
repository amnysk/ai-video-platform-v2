"""手動承認の資格と、flag OFF での未結線（ADR-0081 / INV-129, 130）。

テストの理由は docs/testing/v3-phase3-quality.md。
"""

from __future__ import annotations

import pytest

from contracts.approval import ApproverKind
from contracts.quality import QualityReason
from domain.quality.approval import UploadApprover, review_release_refusal
from infrastructure.config import Settings
from infrastructure.quality.wiring import build_quality_service


def test_only_a_well_formed_owner_can_release_a_review() -> None:
    assert review_release_refusal(UploadApprover(ApproverKind.OWNER, "owner-1")) is None


def test_a_delegated_agent_cannot_release_a_review() -> None:
    refusal = review_release_refusal(UploadApprover(ApproverKind.AGENT_DELEGATED, "agent-1"))
    assert refusal is QualityReason.APPROVER_NOT_OWNER


@pytest.mark.parametrize(
    "approver",
    [
        None,
        "owner",
        {"kind": "owner", "approver_id": "x"},
        UploadApprover("owner", "owner-1"),  # type: ignore[arg-type]  # 文字列は種別ではない
        UploadApprover(ApproverKind.OWNER, ""),
        UploadApprover(ApproverKind.OWNER, "a b"),
        UploadApprover(ApproverKind.OWNER, "x" * 200),
    ],
    ids=["none", "str", "dict", "str_kind", "empty_id", "unsafe_id", "too_long"],
)
def test_anything_unknown_is_refused_and_never_raises(approver: object) -> None:
    assert review_release_refusal(approver) is QualityReason.APPROVER_UNKNOWN


def test_with_the_flag_off_no_service_is_built_and_no_store_is_created(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import infrastructure.quality.wiring as wiring

    def explode(*_a: object, **_k: object) -> None:
        raise AssertionError("MinIO client must not be created while the flag is OFF")

    monkeypatch.setattr(wiring.MinioArtifactStore, "from_settings", explode)
    assert build_quality_service(Settings(_env_file=None), object()) is None  # type: ignore[arg-type]


def test_with_the_flag_on_a_service_is_built_from_the_one_wiring_function(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import infrastructure.quality.wiring as wiring
    from contracts.quality import QUALITY_GATE_ENFORCE_ENV
    from infrastructure.storage.memory_store import InMemoryArtifactStore

    monkeypatch.setattr(
        wiring.MinioArtifactStore, "from_settings", lambda _s: InMemoryArtifactStore()
    )
    settings = Settings(_env_file=None, **{QUALITY_GATE_ENFORCE_ENV: True})  # type: ignore[arg-type]
    assert build_quality_service(settings, object()) is not None  # type: ignore[arg-type]
