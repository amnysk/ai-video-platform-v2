"""ProposeStoryboardChange（fake port）。理由は docs/testing/jpmas2-p0b-rationale.md。"""

from __future__ import annotations

import uuid
from collections.abc import Sequence

import pytest

from application.errors import OperationError, OperationErrorCode
from application.ports import Caller, ReceivedCommand
from application.use_cases.propose_storyboard_change import ProposeStoryboardChange
from contracts.application import (
    ProposeStoryboardChangeRequest,
    storyboard_edit_command_id,
)
from contracts.intervention import (
    ActorType,
    CommandEnvelope,
    CommandStatus,
    CommandType,
    ProposeChangePayload,
    Scope,
)

EP = uuid.uuid4()


class FakePort:
    def __init__(self) -> None:
        self.received: list[list[CommandEnvelope]] = []
        self.started: list[list[str]] = []
        self.created = True

    async def receive(self, episode_id: str, envelopes: Sequence[CommandEnvelope], caller: Caller):
        self.received.append(list(envelopes))
        return [
            ReceivedCommand(e.command_id, CommandStatus.RECEIVED, self.created) for e in envelopes
        ]

    async def start(self, command_ids: Sequence[str]) -> None:
        self.started.append(list(command_ids))


def caller(*scopes: Scope) -> Caller:
    return Caller(ActorType.AGENT, "a", frozenset(scopes or {Scope.READ, Scope.PROPOSE}))


def request(edits: list[dict] | None = None, **extra: object) -> ProposeStoryboardChangeRequest:
    return ProposeStoryboardChangeRequest.model_validate(
        {
            "command_id": "req-00000001",
            "proposal": {
                "episode_id": str(EP),
                "base_storyboard_revision_id": "rev-1",
                "base_manifest_id": "man-1",
                "rationale": "clarify",
                "edits": edits or [{"scene_id": "s1", "visual_description": "castle"}],
                **extra,
            },
            "expected_control_revision": 3,
        }
    )


async def test_one_propose_command_per_edit_with_a_validated_payload() -> None:
    port = FakePort()
    edits = [
        {"scene_id": "s1", "visual_description": "castle"},
        {"scene_id": "s2", "visual_description": "river"},
    ]
    result = await ProposeStoryboardChange(port).execute(caller(), EP, request(edits))
    [envelopes] = port.received
    assert [e.type for e in envelopes] == [CommandType.PROPOSE_CHANGE] * 2
    assert [e.command_id for e in envelopes] == [
        storyboard_edit_command_id("req-00000001", i) for i in (0, 1)
    ]
    assert {e.expected_control_revision for e in envelopes} == {3}
    payload = ProposeChangePayload.model_validate(envelopes[1].payload)
    assert payload.change.kind.value == "scene.override_visual"
    assert payload.change.scene_id == "s2"  # type: ignore[union-attr]
    assert payload.base_manifest_id == "man-1" and payload.rationale == "clarify"
    assert port.started == [[e.command_id for e in envelopes]]
    assert [c.scene_id for c in result.commands] == ["s1", "s2"]
    assert all(c.created for c in result.commands)


async def test_a_replay_starts_nothing() -> None:
    port = FakePort()
    port.created = False
    result = await ProposeStoryboardChange(port).execute(caller(), EP, request())
    assert port.started == [] and not result.commands[0].created


@pytest.mark.parametrize(
    ("extra", "field"),
    [
        ({"framing": "wide"}, "framing"),
        ({"camera_movement": "push"}, "camera_movement"),
        ({"visual_kind": "generated"}, "visual_kind"),
        ({"reference_asset_id": "a"}, "reference_asset_id"),
        ({"visual_description": None}, "visual_description"),
    ],
)
async def test_unsupported_edits_are_refused_before_the_port(extra: dict, field: str) -> None:
    port = FakePort()
    edit = {"scene_id": "s1", "visual_description": "x", **extra}
    with pytest.raises(OperationError) as err:
        await ProposeStoryboardChange(port).execute(caller(), EP, request([edit]))
    assert err.value.code is OperationErrorCode.UNSUPPORTED_CHANGE
    assert field in str(err.value.detail["fields"])
    assert "storyboard.patch_scenes" in err.value.message
    assert port.received == []


async def test_duplicate_scenes_mismatched_episode_and_missing_scope() -> None:
    port = FakePort()
    dup = [{"scene_id": "s1", "visual_description": "a"}] * 2
    with pytest.raises(OperationError) as err:
        await ProposeStoryboardChange(port).execute(caller(), EP, request(dup))
    assert err.value.code is OperationErrorCode.INVALID_PAYLOAD
    with pytest.raises(OperationError) as err:
        await ProposeStoryboardChange(port).execute(caller(), uuid.uuid4(), request())
    assert err.value.code is OperationErrorCode.INVALID_PAYLOAD
    with pytest.raises(OperationError) as err:
        await ProposeStoryboardChange(port).execute(caller(Scope.READ), EP, request())
    assert err.value.code is OperationErrorCode.MISSING_SCOPE
    assert err.value.detail["required_scope"] == "propose"
    assert port.received == []


async def test_every_command_of_a_request_carries_the_whole_request_fingerprint() -> None:
    """件数違いの再送を先頭 command の指紋で拒否できるよう、全 command が同じ指紋を持つ。"""
    one = FakePort()
    two = FakePort()
    edits = [
        {"scene_id": "s1", "visual_description": "castle"},
        {"scene_id": "s2", "visual_description": "river"},
    ]
    await ProposeStoryboardChange(one).execute(caller(), EP, request(edits))
    await ProposeStoryboardChange(two).execute(caller(), EP, request(edits[:1]))
    shas = {ProposeChangePayload.model_validate(e.payload).request_sha256 for e in one.received[0]}
    assert len(shas) == 1 and None not in shas
    # 先頭の edit が同じでも、件数が違えば先頭 command の payload（= 指紋）が変わる
    assert one.received[0][0].payload != two.received[0][0].payload
    assert one.received[0][0].command_id == two.received[0][0].command_id
