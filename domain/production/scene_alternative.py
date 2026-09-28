"""拒否されたシーンの代替映像案: 検証規則・上限判定・planner の port（ADR-0035）。純粋関数のみ。

planner（LLM）は案を**提案**するだけで、採否はここの決定的な規則が決める:

- provider が内容方針（``content_policy_violation``）で拒否したシーンでは、人物を画面の主題に
  する映像対象（``named_person`` / ``figure_anonymous``）を選ばない。拒否理由は実在人物の肖像で、
  判定器は非公開なので、人物を主題にしない方向へ倒す（保守側）
- 元のシーン・過去の案と同じ文面は不可（同じ判定を繰り返すだけ）
- 史実を損なわないことの根拠（``rationale``）が無い案は不可
- 自動の試行回数・追加費用には上限がある（INV-34）。上限・不成立・検証失敗はすべて
  理由つきの needs_input で止まり、人間の判断を待つ
"""

from __future__ import annotations

import json
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from typing import Protocol, runtime_checkable

from contracts.artifacts import StoryboardScene, StoryboardVisualKind, VisualSubject
from contracts.production_activities import (
    MAX_RECOVERY_COST_USD_PER_EPISODE,
    MAX_SCENE_ALTERNATIVES_PER_EPISODE,
    MAX_SCENE_ALTERNATIVES_PER_SCENE,
)
from contracts.states import RejectedInput
from domain.artifact.hashing import canonical_json_bytes, sha256_hex
from domain.errors import SceneAlternativeInvalidError, SceneAlternativeLimitReachedError

__all__ = [
    "CONTENT_POLICY_ERROR_TYPE",
    "PERSON_SUBJECTS",
    "CostEntry",
    "PlannerRawOutput",
    "PreviousAlternative",
    "RejectionFact",
    "SceneAlternativeContext",
    "SceneAlternativeInfeasible",
    "SceneAlternativePlanner",
    "SceneAlternativeProposal",
    "allowed_subjects",
    "check_recovery_limits",
    "parse_planner_output",
    "recovery_cost_usd",
    "scene_alternative_input_hash",
    "validate_proposal",
]

CONTENT_POLICY_ERROR_TYPE = "content_policy_violation"
#: 人物を画面の主題にする映像対象。内容方針で拒否されたシーンの代替案では選ばない。
PERSON_SUBJECTS: frozenset[VisualSubject] = frozenset(
    {VisualSubject.NAMED_PERSON, VisualSubject.FIGURE_ANONYMOUS}
)
#: planner の出力の上限（contracts の SceneVisualOverrideArtifact と同じ値の範囲に収まるよう
#: 検証は Artifact の build でも行う。ここは JSON として解釈できるかの検査）。
_MAX_PLANNER_OUTPUT_CHARS = 20_000


@dataclass(frozen=True, slots=True)
class RejectionFact:
    """planner と規則に渡す拒否1件（``provider_rejections`` の行から）。"""

    id: str
    rejected_input: RejectedInput
    types: tuple[str, ...]
    reason: str | None
    message: str | None


@dataclass(frozen=True, slots=True)
class PreviousAlternative:
    revision: int
    visual_subject: VisualSubject
    visual_description: str


@dataclass(frozen=True, slots=True)
class SceneAlternativeContext:
    """planner への入力。``scene`` は今の実効シーン（過去の案を適用済み）。"""

    episode_id: str
    scene: StoryboardScene
    #: storyboard が最初に決めた映像指示（過去の案で上書きされる前）
    original_description: str
    #: このシーンが属する台本シーンのナレーション（名前・功績はこちらで伝わる）
    narration: str
    language: str
    rejections: tuple[RejectionFact, ...]
    previous: tuple[PreviousAlternative, ...]
    allowed_subjects: tuple[VisualSubject, ...]


@dataclass(frozen=True, slots=True)
class SceneAlternativeProposal:
    visual_kind: StoryboardVisualKind
    visual_subject: VisualSubject
    visual_description: str
    framing: str | None
    camera_movement: str | None
    rationale: str


@dataclass(frozen=True, slots=True)
class SceneAlternativeInfeasible:
    """史実を損なわずに方針に合う案を作れない、という planner の判断（理由つき）。"""

    reason: str


@dataclass(frozen=True, slots=True)
class PlannerRawOutput:
    """planner の生出力。解釈はここ（``parse_planner_output``）で行う。"""

    text: str
    model: str


@runtime_checkable
class SceneAlternativePlanner(Protocol):
    """代替映像案を提案するもの。本番は Codex、試験は fake。"""

    @property
    def generator_id(self) -> str: ...

    @property
    def generator_model(self) -> str: ...

    @property
    def generation_profile_id(self) -> str: ...

    async def plan(self, context: SceneAlternativeContext) -> PlannerRawOutput: ...


def allowed_subjects(rejections: Iterable[RejectionFact]) -> tuple[VisualSubject, ...]:
    """このシーンの代替案で選んでよい映像対象（語彙の定義順）。"""
    policy_rejected = any(CONTENT_POLICY_ERROR_TYPE in r.types for r in rejections)
    return tuple(s for s in VisualSubject if not (policy_rejected and s in PERSON_SUBJECTS))


def _normalized(text: str) -> str:
    return " ".join(text.split()).casefold()


def parse_planner_output(text: str) -> SceneAlternativeProposal | SceneAlternativeInfeasible:
    """planner の JSON を解釈する。形式の欠陥は修復せず ``SceneAlternativeInvalidError``。"""
    if len(text) > _MAX_PLANNER_OUTPUT_CHARS:
        raise SceneAlternativeInvalidError("planner output is too long")
    start, end = text.find("{"), text.rfind("}")
    if start < 0 or end <= start:
        raise SceneAlternativeInvalidError("planner output has no JSON object")
    try:
        data = json.loads(text[start : end + 1])
    except ValueError as exc:
        raise SceneAlternativeInvalidError(f"planner output is not JSON: {exc}") from exc
    if not isinstance(data, dict):
        raise SceneAlternativeInvalidError("planner output is not a JSON object")
    feasible = data.get("feasible")
    if feasible is False:
        reason = data.get("reason")
        if not isinstance(reason, str) or not reason.strip():
            raise SceneAlternativeInvalidError("infeasible plan without a reason")
        return SceneAlternativeInfeasible(reason=reason.strip()[:1000])
    if feasible is not True:
        raise SceneAlternativeInvalidError("planner output must set feasible to true or false")
    try:
        return SceneAlternativeProposal(
            visual_kind=StoryboardVisualKind(data["visual_kind"]),
            visual_subject=VisualSubject(data["visual_subject"]),
            visual_description=str(data["visual_description"]).strip(),
            framing=_optional_text(data.get("framing")),
            camera_movement=_optional_text(data.get("camera_movement")),
            rationale=str(data.get("rationale") or "").strip(),
        )
    except (KeyError, ValueError) as exc:
        raise SceneAlternativeInvalidError(f"planner output is incomplete: {exc}") from exc


def _optional_text(value: object) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def validate_proposal(proposal: SceneAlternativeProposal, context: SceneAlternativeContext) -> None:
    """採否の規則。落ちれば ``SceneAlternativeInvalidError``（自動では作り直さない）。"""
    if proposal.visual_subject not in context.allowed_subjects:
        raise SceneAlternativeInvalidError(
            f"visual_subject {proposal.visual_subject.value!r} is not allowed after this "
            f"rejection (allowed: {[s.value for s in context.allowed_subjects]})"
        )
    if not proposal.visual_description:
        raise SceneAlternativeInvalidError("empty visual_description")
    if not proposal.rationale:
        raise SceneAlternativeInvalidError(
            "the plan does not explain why it keeps the facts and addresses the rejection"
        )
    seen = {
        _normalized(context.original_description),
        _normalized(context.scene.visual_description),
        *(_normalized(p.visual_description) for p in context.previous),
    }
    if _normalized(proposal.visual_description) in seen:
        raise SceneAlternativeInvalidError(
            "the plan repeats a visual description that was already tried"
        )


@dataclass(frozen=True, slots=True)
class CostEntry:
    """有料のメディア予約1件の見積り額（復旧費用の材料）。"""

    scene_id: str | None
    estimated_cost_usd: Decimal
    input_rejected_by_provider: bool
    reserved_at: datetime


def recovery_cost_usd(
    entries: Sequence[CostEntry], first_alternative_at: Mapping[str, datetime]
) -> Decimal:
    """内容拒否からの復旧に使った（とみなす）費用。

    - provider に拒否された予約（拒否時の課金は文書化されていないので課金された前提）
    - 代替案を作ったシーンの、最初の代替案より後に作った予約（作り直しの費用）
    """
    total = Decimal("0")
    for entry in entries:
        started = first_alternative_at.get(entry.scene_id or "")
        rebuilt = started is not None and entry.reserved_at >= started
        if entry.input_rejected_by_provider or rebuilt:
            total += entry.estimated_cost_usd
    return total


def check_recovery_limits(
    *,
    scene_alternatives: int,
    episode_alternatives: int,
    recovery_cost: Decimal,
    projected_cost: Decimal,
    max_per_scene: int = MAX_SCENE_ALTERNATIVES_PER_SCENE,
    max_per_episode: int = MAX_SCENE_ALTERNATIVES_PER_EPISODE,
    max_cost_usd: float = MAX_RECOVERY_COST_USD_PER_EPISODE,
) -> None:
    """次の代替案を作ってよいか（INV-34）。回数・費用は DB から数えた値を渡す。"""
    if scene_alternatives >= max_per_scene:
        raise SceneAlternativeLimitReachedError(
            f"this scene already had {scene_alternatives} automatic alternative(s) "
            f"(limit {max_per_scene}); a human needs to decide"
        )
    if episode_alternatives >= max_per_episode:
        raise SceneAlternativeLimitReachedError(
            f"this episode already had {episode_alternatives} automatic alternative(s) "
            f"(limit {max_per_episode}); a human needs to decide"
        )
    cap = Decimal(str(max_cost_usd))
    if recovery_cost + projected_cost > cap:
        raise SceneAlternativeLimitReachedError(
            f"recovery cost so far ${recovery_cost} plus the next attempt ${projected_cost} "
            f"would exceed the ${cap} per-episode cap"
        )


def scene_alternative_input_hash(
    *,
    episode_id: str,
    scene_id: str,
    scene_fingerprint: str,
    rejection_ids: Sequence[str],
    previous_override_sha256s: Sequence[str],
    planner_profile_id: str,
) -> str:
    """planner 呼び出しの入力指紋（予約台帳の冪等キーの材料）。"""
    return sha256_hex(
        canonical_json_bytes(
            {
                "episode_id": episode_id,
                "scene_id": scene_id,
                "scene_fingerprint": scene_fingerprint,
                "rejection_ids": sorted(rejection_ids),
                "previous_override_sha256s": sorted(previous_override_sha256s),
                "planner_profile_id": planner_profile_id,
            }
        )
    )
