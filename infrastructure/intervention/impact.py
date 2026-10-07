"""ChangeSet の影響範囲と見積り（``docs/domain/intervention.md`` §7、設計 §9 / §12）。

素材の**再生成**範囲と、品質を**再確認**する範囲を分ける（設計 §12）。有料の見積りは、その
シーンの画像・動画を前回送ったときに provider adapter 自身が予約台帳へ書いた見積り
（``provider_reservations.estimated_cost_usd``。同じシーン・同じ尺なので同じ式の値）を使う。
fal adapter を直接 import しない（INV-18: 有料 adapter の入口を数えられる場所に限る）。
台帳に材料が無ければ見積りは不明（None）で、予算を検証できない変更として扱う。
ローカル TTS・render は 0、LLM は記録が無いので ``unmeasured``（0 として足さない）。

- ``script.edit_scenes``: 再生成（現行から降ろす）= 全シーンの音声・production_manifest・
  final_video。再利用 = 画像・動画・storyboard（結び直し）。有料の見積り 0
- ``scene.override_visual``: 再生成 = そのシーンの画像・動画・production_manifest・final_video。
  再利用 = 他シーン（INV-33）。有料の見積り = 画像1枚 + 動画（シーン尺）

音声の入力指紋は台本全体を含むので、台本の修正は全シーンの音声を作り直す（無料。ADR-0041 の負債）。
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any

from contracts.intervention import ChangeKind
from contracts.pipeline import PipelineStage
from contracts.states import ArtifactType
from infrastructure.intervention.budget import UNMEASURED
from infrastructure.intervention.store import manifest_slot

#: 変更が降ろす（再生成させる）Episode 単位の下流
_EPISODE_DOWNSTREAM: tuple[ArtifactType, ...] = (
    ArtifactType.PRODUCTION_MANIFEST,
    ArtifactType.FINAL_VIDEO,
)


@dataclass(frozen=True, slots=True)
class ConstraintRule:
    """``annotations(kind=user_constraint)`` の検証できる部分。"""

    annotation_id: str
    rule: str
    target_ids: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class Impact:
    kind: ChangeKind
    #: 新しい revision ができる slot
    target_slot: str
    #: 現行から降ろして作り直す slot（``manifest_slot`` の形）
    regenerate: tuple[str, ...]
    #: 作り直さず使い続ける slot
    reuse: tuple[str, ...]
    #: 採用 manifest に結び付いていて再確認が要る approval（annotation id）
    rereview: tuple[str, ...]
    #: None = 有料の再生成を伴うが見積りの材料が無い（予算を検証できない。0 として扱わない）
    estimated_paid_cost_usd: Decimal | None
    cost_breakdown: tuple[dict[str, Any], ...]
    #: 再開時に最低限ここから走らせる工程
    restart_stage: PipelineStage
    constraint_conflict: bool = False
    constraint_violations: tuple[str, ...] = field(default_factory=tuple)

    def as_dict(self) -> dict[str, Any]:
        return {
            "kind": self.kind.value,
            "target_slot": self.target_slot,
            "regenerate": list(self.regenerate),
            "reuse": list(self.reuse),
            "rereview": list(self.rereview),
            "estimated_paid_cost_usd": (
                None if self.estimated_paid_cost_usd is None else str(self.estimated_paid_cost_usd)
            ),
            "cost_breakdown": [dict(item) for item in self.cost_breakdown],
            "llm_cost": UNMEASURED,
            "local_cost": "0 (local TTS / render)",
            "restart_stage": self.restart_stage.value,
            "constraint_conflict": self.constraint_conflict,
            "constraint_violations": list(self.constraint_violations),
        }


def _episode_downstream(members: Mapping[str, str]) -> list[str]:
    return [manifest_slot(t) for t in _EPISODE_DOWNSTREAM if manifest_slot(t) in members]


def _prefixed(members: Mapping[str, str], artifact_type: ArtifactType) -> list[str]:
    prefix = f"{artifact_type.value}:"
    return sorted(slot for slot in members if slot.startswith(prefix))


def script_edit_impact(
    *,
    members: Mapping[str, str],
    edited_segment_ids: Sequence[str],
    constraints: Sequence[ConstraintRule],
    approvals: Sequence[str],
) -> Impact:
    voices = _prefixed(members, ArtifactType.SCENE_VOICE)
    reuse = [
        slot
        for slot in members
        if slot.startswith(
            (
                f"{ArtifactType.SCENE_IMAGE.value}:",
                f"{ArtifactType.SCENE_VIDEO.value}:",
                f"{ArtifactType.SCENE_VISUAL_OVERRIDE.value}:",
            )
        )
    ]
    if manifest_slot(ArtifactType.STORYBOARD) in members:
        reuse.append(manifest_slot(ArtifactType.STORYBOARD))
    violations = tuple(
        f"{c.annotation_id}:{c.rule}:{target}"
        for c in constraints
        if c.rule == "keep_segment_text"
        for target in c.target_ids
        if target in edited_segment_ids
    )
    # storyboard が未作成なら storyboard から（台本の修正で storyboard 以前に戻る）
    restart = (
        PipelineStage.PRODUCTION
        if manifest_slot(ArtifactType.STORYBOARD) in members
        else PipelineStage.STORYBOARD
    )
    return Impact(
        kind=ChangeKind.SCRIPT_EDIT_SCENES,
        target_slot=manifest_slot(ArtifactType.SCRIPT),
        regenerate=tuple(voices + _episode_downstream(members)),
        reuse=tuple(sorted(reuse)),
        rereview=tuple(approvals),
        estimated_paid_cost_usd=Decimal("0"),
        cost_breakdown=(
            {"item": "scene_voice", "count": len(voices), "usd": "0", "basis": "local TTS"},
        ),
        restart_stage=restart,
        constraint_conflict=bool(violations),
        constraint_violations=violations,
    )


def scene_override_impact(
    *,
    members: Mapping[str, str],
    scene_id: str,
    image_usd: Decimal | None,
    video_usd: Decimal | None,
    constraints: Sequence[ConstraintRule],
    approvals: Sequence[str],
) -> Impact:
    scene_slots = [
        manifest_slot(ArtifactType.SCENE_IMAGE, scene_id),
        manifest_slot(ArtifactType.SCENE_VIDEO, scene_id),
    ]
    reuse = [
        slot
        for slot in members
        if ":" in slot and slot.split(":", 1)[1] != scene_id and not slot.startswith("scene_voice")
    ]
    reuse += _prefixed(members, ArtifactType.SCENE_VOICE)
    total = None if image_usd is None or video_usd is None else image_usd + video_usd
    violations = tuple(
        f"{c.annotation_id}:{c.rule}:{target}"
        for c in constraints
        if c.rule == "keep_scene_visual"
        for target in c.target_ids
        if target == scene_id
    )
    return Impact(
        kind=ChangeKind.SCENE_OVERRIDE_VISUAL,
        target_slot=manifest_slot(ArtifactType.SCENE_VISUAL_OVERRIDE, scene_id),
        regenerate=tuple(scene_slots + _episode_downstream(members)),
        reuse=tuple(sorted(set(reuse))),
        rereview=tuple(approvals),
        estimated_paid_cost_usd=total,
        cost_breakdown=(
            {
                "item": "scene_image",
                "scene_id": scene_id,
                "usd": None if image_usd is None else str(image_usd),
                "basis": "latest provider estimate for this scene (ledger)",
            },
            {
                "item": "scene_video",
                "scene_id": scene_id,
                "usd": None if video_usd is None else str(video_usd),
                "basis": "latest provider estimate for this scene (ledger)",
            },
        ),
        restart_stage=PipelineStage.PRODUCTION,
        constraint_conflict=bool(violations),
        constraint_violations=violations,
    )
