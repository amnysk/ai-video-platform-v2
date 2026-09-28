"""シーンの映像対象を Storyboard の時点で決める規則（ADR-0035 (1)）。純粋関数のみ。

根拠: 2026-09-26/27 に provider が拒否した入力画像は、どちらも写実的で顔が識別できる人物が
画面の主題だった（拒否理由 "likenesses of real people"）。人物を一律に禁じるのではなく、
人物を「肖像として撮る」種類の構図（``talking_head`` / ``character``）で計画させない。
"""

from __future__ import annotations

from contracts.artifacts import StoryboardVisualKind, VisualSubject
from domain.errors import StoryboardSchemaViolationError

__all__ = [
    "GENERATE_SOURCE",
    "PEOPLE_SUBJECTS",
    "PORTRAIT_VISUAL_KINDS",
    "check_subject_composition",
    "subject_from_required_assets",
]

#: 外部 scene_plan の ``required_assets[].source`` のうち、このシーンのために生成する素材。
GENERATE_SOURCE = "generate"

#: 人物が画面に入る映像対象。
PEOPLE_SUBJECTS: frozenset[VisualSubject] = frozenset(
    {VisualSubject.NAMED_PERSON, VisualSubject.FIGURE_ANONYMOUS}
)

#: 人物を画面の主題として正面から見せる構図の種類。人物の映像対象とは組み合わせない。
PORTRAIT_VISUAL_KINDS: frozenset[StoryboardVisualKind] = frozenset(
    {StoryboardVisualKind.TALKING_HEAD, StoryboardVisualKind.CHARACTER}
)


def subject_from_required_assets(assets: object, *, label: str) -> VisualSubject:
    """``source == "generate"`` の素材がちょうど1件あり、その ``type`` が語彙にあること。

    違反は LLM 出力の形式不正（retryable、ADR-0014）。推測で補わない。
    """
    if not isinstance(assets, list):
        raise StoryboardSchemaViolationError(
            f"{label}: required_assets must declare the visual subject"
        )
    generated = [a for a in assets if isinstance(a, dict) and a.get("source") == GENERATE_SOURCE]
    if len(generated) != 1:
        raise StoryboardSchemaViolationError(
            f"{label}: required_assets must have exactly one source=generate entry "
            f"(got {len(generated)})"
        )
    raw = generated[0].get("type")
    try:
        return VisualSubject(raw)
    except ValueError as exc:
        raise StoryboardSchemaViolationError(
            f"{label}: required_assets type {raw!r} is not a visual subject"
        ) from exc


def check_subject_composition(
    kind: StoryboardVisualKind, subject: VisualSubject, *, label: str
) -> None:
    """人物の映像対象を、人物を肖像として撮る構図で計画していないこと。"""
    if subject in PEOPLE_SUBJECTS and kind in PORTRAIT_VISUAL_KINDS:
        raise StoryboardSchemaViolationError(
            f"{label}: {subject.value} cannot be planned as a {kind.value} shot; "
            "show the person small, distant or from behind, or choose another subject"
        )
