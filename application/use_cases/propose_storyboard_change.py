"""``ProposeStoryboardChange``（ADR-0043 §1）。proposal から propose_change を作る唯一の場所。

適用はしない（``apply`` は別操作）。P0 は ``visual_description`` だけを変える edit を受け付け、
既存の ``scene.override_visual`` に写す。
"""

from __future__ import annotations

import uuid

from application.errors import OperationError, OperationErrorCode
from application.ports import Caller, CommandPort
from contracts.application import (
    ProposedSceneCommand,
    ProposeStoryboardChangeRequest,
    StoryboardProposalResult,
    storyboard_edit_command_id,
    storyboard_request_sha256,
)
from contracts.intervention import (
    REQUIRED_SCOPE,
    CommandEnvelope,
    CommandType,
    ProposeChangePayload,
    SceneOverrideVisual,
)

_UNSUPPORTED_FIELDS = ("framing", "camera_movement", "visual_kind", "reference_asset_id")


class ProposeStoryboardChange:
    def __init__(self, port: CommandPort) -> None:
        self._port = port

    async def execute(
        self, caller: Caller, episode_id: uuid.UUID, request: ProposeStoryboardChangeRequest
    ) -> StoryboardProposalResult:
        required = REQUIRED_SCOPE[CommandType.PROPOSE_CHANGE]
        if required not in caller.scopes:
            raise OperationError(
                OperationErrorCode.MISSING_SCOPE,
                f"this caller lacks the {required.value!r} scope",
                {"required_scope": required.value},
            )
        proposal = request.proposal
        if proposal.episode_id != episode_id:
            raise OperationError(
                OperationErrorCode.INVALID_PAYLOAD,
                "proposal.episode_id does not match the episode in the path",
            )
        scene_ids = [e.scene_id for e in proposal.edits]
        duplicated = sorted({s for s in scene_ids if scene_ids.count(s) > 1})
        if duplicated:
            raise OperationError(
                OperationErrorCode.INVALID_PAYLOAD,
                "scene_id appears in more than one edit",
                {"duplicate_scene_ids": duplicated},
            )
        for index, edit in enumerate(proposal.edits):
            unsupported = [f for f in _UNSUPPORTED_FIELDS if getattr(edit, f) is not None]
            if edit.visual_description is None:
                unsupported.append("visual_description (missing)")
            if unsupported:
                raise OperationError(
                    OperationErrorCode.UNSUPPORTED_CHANGE,
                    "only visual_description edits are supported; "
                    "storyboard.patch_scenes is not implemented",
                    {"edit_index": index, "scene_id": edit.scene_id, "fields": unsupported},
                )
        request_sha256 = storyboard_request_sha256(
            episode_id, proposal, request.expected_control_revision
        )
        envelopes: list[CommandEnvelope] = []
        for index, edit in enumerate(proposal.edits):
            assert edit.visual_description is not None
            payload = ProposeChangePayload(
                change=SceneOverrideVisual(
                    base_revision_id=proposal.base_storyboard_revision_id,
                    scene_id=edit.scene_id,
                    visual_description=edit.visual_description,
                ),
                rationale=proposal.rationale,
                base_manifest_id=proposal.base_manifest_id,
                request_sha256=request_sha256,
            )
            envelopes.append(
                CommandEnvelope(
                    command_id=storyboard_edit_command_id(request.command_id, index),
                    type=CommandType.PROPOSE_CHANGE,
                    expected_control_revision=request.expected_control_revision,
                    payload=payload.model_dump(mode="json", exclude_none=True),
                    correlation_id=None
                    if caller.correlation_id is None
                    else caller.correlation_id[:64],
                )
            )
        received = await self._port.receive(str(episode_id), envelopes, caller)
        created = [r.command_id for r in received if r.created]
        if created:
            await self._port.start(created)
        return StoryboardProposalResult(
            request_command_id=request.command_id,
            episode_id=episode_id,
            commands=tuple(
                ProposedSceneCommand(
                    scene_id=edit.scene_id,
                    command_id=r.command_id,
                    status=r.status,
                    created=r.created,
                )
                for edit, r in zip(proposal.edits, received, strict=True)
            ),
        )
