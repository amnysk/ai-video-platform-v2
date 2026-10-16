"""版付き Workflow Template（ADR-0043 §3、JP-MAS-002 v1.1 §2.6）。

Template は**ソース管理された Python データ**で、YAML 読込・DB 登録・利用者入力からのコード実行を
しない。コンパイルは ``domain/workflow_templates/compiler.py``。

**この契約に無いもの**: 公開設定・予算上限・Schedule を表す項目（Template から拡張できない）。
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from enum import StrEnum
from typing import Literal

from contracts.agents import AgentHandlerRef

#: QA 修正サイクルの上限。Template がこれを超える値を宣言するとコンパイルが拒否する
HARD_MAX_QA_REPAIR_CYCLES = 2


class ExistingStageHandler(StrEnum):
    PRODUCER_ADOPT = "existing_producer_adopt"
    SCRIPT = "existing_script"
    STORYBOARD = "existing_storyboard"
    PRODUCTION = "existing_production"
    RENDER = "existing_render"
    PRIVATE_UPLOAD = "existing_private_upload"


#: 必須の既存工程とその順序（省略・入れ替え不可）
REQUIRED_STAGE_ORDER: tuple[ExistingStageHandler, ...] = (
    ExistingStageHandler.SCRIPT,
    ExistingStageHandler.STORYBOARD,
    ExistingStageHandler.PRODUCTION,
    ExistingStageHandler.RENDER,
    ExistingStageHandler.PRIVATE_UPLOAD,
)


@dataclass(frozen=True, slots=True)
class TemplateStage:
    id: str
    #: ``AgentHandlerRef`` または ``ExistingStageHandler`` の値
    handler: str
    after: tuple[str, ...] = ()
    #: agent stage の ``role@spec_version``（existing stage は None）
    agent_ref: str | None = None
    #: post_publish stage の起動条件
    trigger: str | None = None


@dataclass(frozen=True, slots=True)
class WorkflowTemplate:
    template_id: str
    version: str
    format_profile: str
    execution: Literal["legacy_pipeline", "agentic"]
    runnable: bool
    stages: tuple[TemplateStage, ...]
    post_publish: tuple[TemplateStage, ...] = ()
    max_qa_repair_cycles: int = 0


def _stage_dict(s: TemplateStage) -> dict[str, object]:
    return {
        "id": s.id,
        "handler": s.handler,
        "after": list(s.after),
        "agent_ref": s.agent_ref,
        "trigger": s.trigger,
    }


def template_hash(template: WorkflowTemplate) -> str:
    body = {
        "template_id": template.template_id,
        "version": template.version,
        "format_profile": template.format_profile,
        "execution": template.execution,
        "runnable": template.runnable,
        "stages": [_stage_dict(s) for s in template.stages],
        "post_publish": [_stage_dict(s) for s in template.post_publish],
        "max_qa_repair_cycles": template.max_qa_repair_cycles,
    }
    text = json.dumps(body, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


_E = ExistingStageHandler


def _existing(id_: str, handler: ExistingStageHandler, *after: str) -> TemplateStage:
    return TemplateStage(id_, handler.value, after)


def _agent(id_: str, handler: AgentHandlerRef, ref: str, *after: str) -> TemplateStage:
    return TemplateStage(id_, handler.value, after, agent_ref=ref)


_QUALITY = _agent("quality", AgentHandlerRef.QUALITY_REVIEWER_V1, "quality_reviewer@1.0")
_LESSON = TemplateStage(
    "lesson",
    AgentHandlerRef.LEARNING_AGENT_V1.value,
    (),
    agent_ref="learning@1.0",
    trigger="public_metrics_available",
)

DAILY_LEGACY_1 = WorkflowTemplate(
    template_id="daily_legacy",
    version="1",
    format_profile="shorts_en",
    execution="legacy_pipeline",
    runnable=True,  # コンパイルできる（execution=legacy_pipeline）。agentic 実行は起動側が拒否する
    stages=(
        _existing("script", _E.SCRIPT),
        _existing("storyboard", _E.STORYBOARD, "script"),
        _existing("production", _E.PRODUCTION, "storyboard"),
        _existing("render", _E.RENDER, "production"),
        _existing("private_upload", _E.PRIVATE_UPLOAD, "render"),
    ),
)

SHORTS_AGENTIC_1 = WorkflowTemplate(
    template_id="shorts_agentic",
    version="1",
    format_profile="shorts_en",
    execution="agentic",
    runnable=True,
    stages=(
        _agent("strategy", AgentHandlerRef.STRATEGY_AGENT_V1, "strategy@1.0"),
        _existing("producer", _E.PRODUCER_ADOPT, "strategy"),
        _agent(
            "creative", AgentHandlerRef.CREATIVE_DIRECTOR_V1, "creative_director@1.0", "producer"
        ),
        _existing("script", _E.SCRIPT, "creative"),
        _existing("storyboard", _E.STORYBOARD, "script"),
        _existing("production", _E.PRODUCTION, "storyboard"),
        _existing("render", _E.RENDER, "production"),
        _existing("private_upload", _E.PRIVATE_UPLOAD, "render"),
        TemplateStage(
            _QUALITY.id, _QUALITY.handler, ("private_upload",), agent_ref=_QUALITY.agent_ref
        ),
    ),
    post_publish=(_LESSON,),
    max_qa_repair_cycles=0,
)

SHORTS_REVIEWED_1 = WorkflowTemplate(
    template_id="shorts_reviewed",
    version="1",
    format_profile="shorts_en",
    execution="agentic",
    runnable=False,  # 未実装。起動は template_not_runnable
    stages=(
        _agent("strategy", AgentHandlerRef.STRATEGY_AGENT_V1, "strategy@1.0"),
        _existing("producer", _E.PRODUCER_ADOPT, "strategy"),
        _agent(
            "creative", AgentHandlerRef.CREATIVE_DIRECTOR_V1, "creative_director@1.0", "producer"
        ),
        _existing("script", _E.SCRIPT, "creative"),
        _existing("storyboard", _E.STORYBOARD, "script"),
        _existing("production", _E.PRODUCTION, "storyboard"),
        _existing("render", _E.RENDER, "production"),
        TemplateStage(_QUALITY.id, _QUALITY.handler, ("render",), agent_ref=_QUALITY.agent_ref),
        _existing("private_upload", _E.PRIVATE_UPLOAD, "quality"),
    ),
    post_publish=(_LESSON,),
    max_qa_repair_cycles=2,
)

TEMPLATES: dict[tuple[str, str], WorkflowTemplate] = {
    (t.template_id, t.version): t for t in (DAILY_LEGACY_1, SHORTS_AGENTIC_1, SHORTS_REVIEWED_1)
}
