"""対話 AI アシスタント向け tool の定義（JP-MAS-001 v2.0 §8 / ADR-0042）。

ここが tool の**唯一の宣言元**（AGENTS.md §8）。名前・説明・入力 JSON Schema・効果の種別・
必要 scope を持つ。command を送る tool の必要 scope は ``contracts.intervention.REQUIRED_SCOPE``
から導き、literal で書き直さない。実行器は ``apps/assistant/`` で、介入 HTTP API だけを呼ぶ。

**予算上限・Schedule・公開設定を変える tool は定義しない**（INV-47 と同じ。
``tests/contract/test_assistant_tools_contract.py`` が検査する）。
入力 schema は ``contracts.intervention`` の payload model の項目だけを使う（API が受け付けない
項目を作らない）。台帳の ``visual``（台本の映像文）の修正はサーバが拒否するので露出しない。
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from enum import StrEnum
from typing import Any

from contracts.application import StoryboardPatchProposal
from contracts.intervention import (
    REQUIRED_SCOPE,
    TARGET_REF_PATTERN,
    AgentRole,
    AnnotationFlag,
    CommandType,
    Scope,
)

#: tool 呼び出しから command_id を決める namespace（INV-49）。変えると再試行の冪等性が切れる
ASSISTANT_NAMESPACE = uuid.UUID("6f1d5c0e-3a52-5b0b-9d0e-0a5d7d9f8c11")

#: 相関 ID（``X-Correlation-ID``）と tool 名（``X-Assistant-Tool``）のヘッダ名
CORRELATION_HEADER = "X-Correlation-ID"
TOOL_HEADER = "X-Assistant-Tool"


#: CLI（``scripts/assistant.py``）の接続設定。``scripts/intervene.py`` と同じ環境変数を使う
#: （一致は ``tests/unit/test_assistant_cli.py`` が検査する）
TOKEN_ENV = "AVP_INTERVENTION_TOKEN"
URL_ENV = "AVP_INTERVENTION_URL"
DEFAULT_URL = "http://localhost:8000"


def tool_command_id(session_id: str, tool_call_id: str) -> str:
    """同じ (session, tool_call) は同じ command_id（INV-49）。再試行が二重にならない。"""
    return str(uuid.uuid5(ASSISTANT_NAMESPACE, f"{session_id}:{tool_call_id}"))


def correlation_id(session_id: str, tool_call_id: str) -> str:
    return f"assistant:{session_id}:{tool_call_id}"


class ToolName(StrEnum):
    GET_EPISODE_STATUS = "get_episode_status"
    READ_DECISIONS = "read_decisions"
    EXPLAIN_DECISION = "explain_decision"
    SEARCH_DECISIONS = "search_decisions"
    COMPARE_REVISIONS = "compare_revisions"
    EXPORT_REVIEW_PACKET = "export_review_packet"
    LIST_CHANGE_SETS = "list_change_sets"
    PREVIEW_CHANGE_IMPACT = "preview_change_impact"
    PROPOSE_SCRIPT_EDIT = "propose_script_edit"
    PROPOSE_SCENE_VISUAL_OVERRIDE = "propose_scene_visual_override"
    ADD_COMMENT = "add_comment"
    REQUEST_HOLD = "request_hold"
    GET_COMMAND_STATUS = "get_command_status"
    APPLY_CHANGE = "apply_change"
    RESUME_EPISODE = "resume_episode"
    #: ADR-0043 §1。入力 schema は ``StoryboardPatchProposal`` から導出する
    PROPOSE_STORYBOARD_PATCH = "propose_storyboard_patch"


TOOL_NAMES: frozenset[str] = frozenset(n.value for n in ToolName)


class ToolEffect(StrEnum):
    READ = "read"  # 何も変えない
    ANNOTATE = "annotate"  # 注記（コメント）を足す。生成・課金を始めない
    PROPOSE = "propose"  # ChangeSet を作るだけ。生成・課金を始めない
    CONTROL = "control"  # 停止・再開
    APPLY = "apply"  # ChangeSet の適用（有料の再生成を起こしうる）


@dataclass(frozen=True, slots=True)
class ToolSpec:
    name: ToolName
    description: str
    input_schema: dict[str, Any]
    effect: ToolEffect
    #: command を送る tool の型。read の tool は None
    command_type: CommandType | None = None

    @property
    def required_scope(self) -> Scope:
        return Scope.READ if self.command_type is None else REQUIRED_SCOPE[self.command_type]


# --------------------------------------------------------------------------- schema の部品

_UUID: dict[str, Any] = {"type": "string", "format": "uuid"}
_EPISODE_ID: dict[str, Any] = {**_UUID, "description": "Episode id."}


def _object(
    properties: dict[str, Any], required: list[str] | None = None, **extra: Any
) -> dict[str, Any]:
    return {
        "type": "object",
        "properties": properties,
        "required": required or [],
        "additionalProperties": False,
        **extra,
    }


def _episode_only() -> dict[str, Any]:
    return _object({"episode_id": _EPISODE_ID}, ["episode_id"])


#: 楽観的な競合検出（``CommandEnvelope`` の項目）。直近に読んだ版を送る
_EXPECTED: dict[str, Any] = {
    "expected_control_revision": {
        "type": "integer",
        "minimum": 0,
        "description": "The control_revision you last read (get_episode_status). The command "
        "conflicts instead of applying if the episode changed since.",
    },
    "expected_target_revision": {
        "type": "string",
        "description": "The revision id you last read for the target. The command conflicts "
        "instead of applying if it is no longer current.",
    },
}

_CONTROL_REVISION: dict[str, Any] = {
    "expected_control_revision": _EXPECTED["expected_control_revision"]
}

_RATIONALE: dict[str, Any] = {
    "type": "string",
    "maxLength": 4000,
    "description": "Why this change is proposed (recorded as the proposer's stated reason).",
}
_BASE_MANIFEST: dict[str, Any] = {
    **_UUID,
    "description": "Optional: the manifest the proposal was made against.",
}
_VARIANT_GROUP: dict[str, Any] = {
    "type": "string",
    "maxLength": 64,
    "description": "Optional: proposals sharing this value are alternatives; applying one "
    "marks the others not_selected.",
}

_TIME_RANGE = _object(
    {
        "start_ms": {"type": "integer", "minimum": 0},
        "end_ms": {"type": "integer", "minimum": 0},
    },
    ["start_ms", "end_ms"],
)
_ANCHOR = _object(
    {
        "segment_id": {"type": "string", "description": "Script scene id (ScriptScene.id)."},
        "scene_id": {"type": "string", "description": "Storyboard scene id."},
        "asset_id": {"type": "string"},
        "time_range": _TIME_RANGE,
        "manifest_id": {"type": "string"},
        "revision_id": {"type": "string", "description": "The revision the comment is about."},
        "target_ref": {
            "type": "string",
            "pattern": TARGET_REF_PATTERN,
            "description": "An agent tool call or decision of this episode: tool_call:<id> or "
            "decision:<id> (ADR-0047).",
        },
    },
    minProperties=1,
    description="What the comment is about. Keeps pointing at that revision even if newer "
    "revisions exist; it is never moved automatically.",
)


def _inline_refs(node: Any, defs: dict[str, Any]) -> Any:
    """``$ref`` を ``$defs`` の中身で置き換える（tool の入力 schema は自己完結にする）。"""
    if isinstance(node, dict):
        if "$ref" in node:
            target = defs[node["$ref"].rsplit("/", 1)[-1]]
            rest = {k: v for k, v in node.items() if k != "$ref"}
            return _inline_refs({**target, **rest}, defs)
        return {k: _inline_refs(v, defs) for k, v in node.items() if k != "$defs"}
    if isinstance(node, list):
        return [_inline_refs(v, defs) for v in node]
    return node


def storyboard_patch_input_schema() -> dict[str, Any]:
    """tool の入力 schema。``StoryboardPatchProposal`` から導出する（手書きしない / INV-52）。

    command_id は tool 呼び出しから決まる（INV-49）ので入力に含めない。
    """
    raw = StoryboardPatchProposal.model_json_schema()
    schema = _inline_refs(raw, raw.get("$defs", {}))
    schema["properties"] = {**schema["properties"], **_CONTROL_REVISION}
    return schema


TOOLS: tuple[ToolSpec, ...] = (
    ToolSpec(
        ToolName.GET_EPISODE_STATUS,
        "Read the current state of an episode: phase, control state (running/held/waiting_review), "
        "mode, adopted revisions, open provider reservations, costs, recent commands and "
        "decisions. "
        "Received commands are NOT applied until their status says applied.",
        _episode_only(),
        ToolEffect.READ,
    ),
    ToolSpec(
        ToolName.READ_DECISIONS,
        "List the recorded decisions of one episode (what was chosen among which candidates, by "
        "which role). Stated rationales are the model's self-report, not verified facts.",
        _episode_only(),
        ToolEffect.READ,
    ),
    ToolSpec(
        ToolName.EXPLAIN_DECISION,
        "Explain one recorded decision from stored records only: question, candidates, the choice, "
        "rationale (with its kind and provenance), evidence refs and their check result, "
        "role/model/"
        "prompt version, constraints, uncertainties, interventions on the same revisions and "
        "downstream revisions. Anything not recorded is listed in not_recorded - never guess it.",
        _object({"episode_id": _EPISODE_ID, "decision_id": _UUID}, ["episode_id", "decision_id"]),
        ToolEffect.READ,
    ),
    ToolSpec(
        ToolName.SEARCH_DECISIONS,
        "Search decision history across episodes (newest first) to learn from past production "
        "results. Page with before = next_before of the previous page.",
        _object(
            {
                "decision_type": {"type": "string", "maxLength": 64},
                "role": {"enum": [r.value for r in AgentRole]},
                "episode_id": _EPISODE_ID,
                "scene_id": {"type": "string", "maxLength": 255},
                "limit": {"type": "integer", "minimum": 1, "maximum": 200},
                "before": {"type": "string", "maxLength": 64},
            }
        ),
        ToolEffect.READ,
    ),
    ToolSpec(
        ToolName.COMPARE_REVISIONS,
        "Compare two revisions of an episode (field-level differences).",
        _object(
            {"episode_id": _EPISODE_ID, "revision_a": _UUID, "revision_b": _UUID},
            ["episode_id", "revision_a", "revision_b"],
        ),
        ToolEffect.READ,
    ),
    ToolSpec(
        ToolName.EXPORT_REVIEW_PACKET,
        "Export the review packet of an episode (script, storyboard, evidence, decisions, costs) "
        "for a person to review.",
        _object(
            {"episode_id": _EPISODE_ID, "format": {"enum": ["json", "markdown"]}}, ["episode_id"]
        ),
        ToolEffect.READ,
    ),
    ToolSpec(
        ToolName.LIST_CHANGE_SETS,
        "List the change sets (proposals) of an episode with status, impact and estimated cost. "
        "An estimated cost of null means not estimated - it is not zero.",
        _episode_only(),
        ToolEffect.READ,
    ),
    ToolSpec(
        ToolName.PREVIEW_CHANGE_IMPACT,
        "Show one change set: the proposed differences, the impact (what becomes stale, what is "
        "regenerated, what is reused) and the estimated cost in USD (null = not estimated). "
        "Use this "
        "to tell the user what applying would cost BEFORE calling apply_change.",
        _object(
            {"episode_id": _EPISODE_ID, "change_set_id": _UUID}, ["episode_id", "change_set_id"]
        ),
        ToolEffect.READ,
    ),
    ToolSpec(
        ToolName.PROPOSE_SCRIPT_EDIT,
        "Propose edits to the script narration/title/hook as a change set (kind "
        "script.edit_scenes). "
        "This only records a proposal; nothing is generated or charged until a change is applied. "
        "To change what a scene looks like use propose_scene_visual_override.",
        _object(
            {
                "episode_id": _EPISODE_ID,
                "base_revision_id": {**_UUID, "description": "The script revision being edited."},
                "title": {"type": "string", "minLength": 1, "maxLength": 100},
                "hook": {"type": "string", "minLength": 1, "maxLength": 80},
                "edits": {
                    "type": "array",
                    "items": _object(
                        {
                            "segment_id": {"type": "string"},
                            "narration": {"type": "string", "minLength": 1, "maxLength": 220},
                        },
                        ["segment_id"],
                    ),
                },
                "rationale": _RATIONALE,
                "base_manifest_id": _BASE_MANIFEST,
                "variant_group": _VARIANT_GROUP,
                **_EXPECTED,
            },
            ["episode_id", "base_revision_id"],
        ),
        ToolEffect.PROPOSE,
        CommandType.PROPOSE_CHANGE,
    ),
    ToolSpec(
        ToolName.PROPOSE_SCENE_VISUAL_OVERRIDE,
        "Propose a new visual description for one storyboard scene as a change set "
        "(kind scene.override_visual). This only records a proposal; applying it regenerates the "
        "scene's image/video and usually costs money.",
        _object(
            {
                "episode_id": _EPISODE_ID,
                "base_revision_id": {**_UUID, "description": "The storyboard revision."},
                "scene_id": {"type": "string"},
                "visual_description": {"type": "string", "minLength": 1, "maxLength": 600},
                "rationale": _RATIONALE,
                "base_manifest_id": _BASE_MANIFEST,
                "variant_group": _VARIANT_GROUP,
                **_EXPECTED,
            },
            ["episode_id", "base_revision_id", "scene_id", "visual_description"],
        ),
        ToolEffect.PROPOSE,
        CommandType.PROPOSE_CHANGE,
    ),
    ToolSpec(
        ToolName.ADD_COMMENT,
        "Add a comment anchored to a script segment, storyboard scene, asset, time range or "
        "manifest. A comment records feedback only; it changes nothing and starts nothing.",
        _object(
            {
                "episode_id": _EPISODE_ID,
                "anchor": _ANCHOR,
                "body": {"type": "string", "minLength": 1, "maxLength": 4000},
                "kind": {"enum": ["comment", "review_finding"]},
                "flag": {
                    "enum": [f.value for f in AnnotationFlag],
                    "description": "Structured finding about anchor.target_ref (needs "
                    "kind=review_finding): wrong_evidence for a tool_call, wrong_decision for a "
                    "decision.",
                },
            },
            ["episode_id", "anchor", "body"],
        ),
        ToolEffect.ANNOTATE,
        CommandType.COMMENT,
    ),
    ToolSpec(
        ToolName.REQUEST_HOLD,
        "Ask the pipeline to stop before sending anything new. A hold request is not an immediate "
        "stop: already-dispatched provider jobs may still complete and be charged. Check "
        "get_episode_status for held.",
        _object(
            {
                "episode_id": _EPISODE_ID,
                "reason": {"type": "string", "maxLength": 1000},
                **_EXPECTED,
            },
            ["episode_id"],
        ),
        ToolEffect.CONTROL,
        CommandType.REQUEST_HOLD,
    ),
    ToolSpec(
        ToolName.GET_COMMAND_STATUS,
        "Read the status of a command. received/validated/applying mean NOT applied yet; only "
        "applied means the change was made. rejected/conflict/failed explain why in reject_reason.",
        _object(
            {"command_id": {"type": "string", "minLength": 8, "maxLength": 64}}, ["command_id"]
        ),
        ToolEffect.READ,
    ),
    ToolSpec(
        ToolName.APPLY_CHANGE,
        "Apply a proposed change set. Call preview_change_impact first, tell the user the "
        "estimated "
        "cost, and pass exactly that number as acknowledged_estimated_cost_usd (pass null "
        "only when "
        "the estimate is null/not estimated). If the cost changed since, nothing is sent "
        "(status cost_changed). AI principals may apply only zero-cost changes; otherwise the "
        "result "
        "is needs_human_approval with the command a person must run.",
        _object(
            {
                "episode_id": _EPISODE_ID,
                "change_set_id": _UUID,
                "acknowledged_estimated_cost_usd": {
                    "type": ["number", "null"],
                    "minimum": 0,
                    "description": "The estimated cost (USD) shown to the user; null only if "
                    "the change set's estimate is null.",
                },
                **_EXPECTED,
            },
            ["episode_id", "change_set_id", "acknowledged_estimated_cost_usd"],
        ),
        ToolEffect.APPLY,
        CommandType.APPLY_CHANGE,
    ),
    ToolSpec(
        ToolName.RESUME_EPISODE,
        "Resume a held episode (or one waiting at a review point). Resuming restarts the pipeline "
        "from the stage decided by what is stale and MAY START PAID REGENERATION. AI-initiated "
        "paid regeneration needs a human: the result is then needs_human_approval with the "
        "command a person must run. Optionally name the start stage.",
        _object(
            {
                "episode_id": _EPISODE_ID,
                "start_stage": {"type": "string", "maxLength": 64},
                **_EXPECTED,
            },
            ["episode_id"],
        ),
        ToolEffect.CONTROL,
        CommandType.RESUME,
    ),
    ToolSpec(
        ToolName.PROPOSE_STORYBOARD_PATCH,
        "Propose new visual descriptions for one or more storyboard scenes (up to 12) as change "
        "sets (kind scene.override_visual, one per scene). This only records proposals; "
        "applying one regenerates the scene's image/video and usually costs money. Only "
        "visual_description edits are supported today: framing, camera_movement, visual_kind and "
        "reference_asset_id are refused with unsupported_change. Needs the propose scope and the "
        "server-side application API to be enabled.",
        storyboard_patch_input_schema(),
        ToolEffect.PROPOSE,
        CommandType.PROPOSE_CHANGE,
    ),
)

TOOLS_BY_NAME: dict[str, ToolSpec] = {t.name.value: t for t in TOOLS}


def tool_use_definitions() -> list[dict[str, Any]]:
    """LLM の tool use にそのまま渡せる形（name / description / input_schema だけ）。"""
    return [
        {"name": t.name.value, "description": t.description, "input_schema": t.input_schema}
        for t in TOOLS
    ]


def catalogue() -> list[dict[str, Any]]:
    """``scripts/assistant.py tools`` の出力。効果と必要 scope を添える。"""
    return [
        {
            "name": t.name.value,
            "description": t.description,
            "input_schema": t.input_schema,
            "effect": t.effect.value,
            "required_scope": t.required_scope.value,
        }
        for t in TOOLS
    ]


__all__ = [
    "ASSISTANT_NAMESPACE",
    "CORRELATION_HEADER",
    "DEFAULT_URL",
    "TOOLS",
    "TOOLS_BY_NAME",
    "TOKEN_ENV",
    "TOOL_HEADER",
    "TOOL_NAMES",
    "ToolEffect",
    "ToolName",
    "ToolSpec",
    "URL_ENV",
    "catalogue",
    "correlation_id",
    "tool_command_id",
    "tool_use_definitions",
]
