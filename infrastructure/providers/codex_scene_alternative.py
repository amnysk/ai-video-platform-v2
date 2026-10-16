"""拒否されたシーンの代替映像案を Codex に提案させる planner（ADR-0035）。

Codex 固有の呼び出し（argv・sandbox・JSONL）は既存の ``CodexCliStoryGenerator`` が持つ。
ここはプロンプト（``prompts/scene_alternative.md``）を組み立てて1回呼ぶだけで、
出力の解釈・採否は ``domain.production.scene_alternative`` が決める（LLM に決めさせない）。
"""

from __future__ import annotations

import json
import re

from domain.artifact.hashing import sha256_hex
from domain.production.scene_alternative import PlannerRawOutput, SceneAlternativeContext
from domain.script.ports import GenerationRequest, StoryGenerator
from prompts import load_prompt_template

PROMPT_TEMPLATE_NAME = "scene_alternative"
GENERATOR_ID = "codex-scene-alternative"
_PLACEHOLDER_RE = re.compile(r"\{\{\s*([a-z0-9_]+)\s*\}\}")


def render_scene_alternative_prompt(template: str, context: SceneAlternativeContext) -> str:
    """``{{ name }}`` を埋める。埋まらないプレースホルダが残れば ``ValueError``。"""
    values = {
        "rejections_json": json.dumps(
            [
                {
                    "rejected_input": r.rejected_input.value,
                    "types": list(r.types),
                    "reason": r.reason,
                    "message": r.message,
                }
                for r in context.rejections
            ],
            ensure_ascii=False,
            indent=2,
        ),
        "narration": context.narration,
        "current_description": context.scene.visual_description,
        "original_description": context.original_description,
        "previous_json": json.dumps(
            [
                {
                    "revision": p.revision,
                    "visual_subject": p.visual_subject.value,
                    "visual_description": p.visual_description,
                }
                for p in context.previous
            ],
            ensure_ascii=False,
        ),
        "duration_ms": str(context.scene.duration_ms),
        "allowed_subjects_json": json.dumps([s.value for s in context.allowed_subjects]),
    }

    def _fill(match: re.Match[str]) -> str:
        name = match.group(1)
        if name not in values:
            raise ValueError(f"unknown placeholder in scene alternative prompt: {name}")
        return values[name]

    return _PLACEHOLDER_RE.sub(_fill, template)


class CodexSceneAlternativePlanner:
    """``SceneAlternativePlanner`` の Codex 実装。"""

    def __init__(self, *, llm: StoryGenerator, model_label: str, timeout_seconds: int) -> None:
        self._llm = llm
        self._model_label = model_label
        self._timeout_seconds = timeout_seconds
        self._template = load_prompt_template(PROMPT_TEMPLATE_NAME)

    @property
    def generator_id(self) -> str:
        return GENERATOR_ID

    @property
    def generator_model(self) -> str:
        return self._model_label

    @property
    def generation_profile_id(self) -> str:
        """テンプレートの内容由来。文面が1文字変われば別の入力として扱う。"""
        return f"{GENERATOR_ID}:{sha256_hex(self._template.encode('utf-8'))[:16]}"

    async def plan(self, context: SceneAlternativeContext) -> PlannerRawOutput:
        prompt = render_scene_alternative_prompt(self._template, context)
        result = await self._llm.generate(
            GenerationRequest(
                episode_id=context.episode_id,
                prompt=prompt,
                output_schema=None,
                timeout_seconds=self._timeout_seconds,
            )
        )
        return PlannerRawOutput(text=result.text, model=result.model)
