"""storyboard の時間軸の正規化（純粋関数）。

生成器（LLM）の時刻は数百ミリ秒ずれることがある。**小さなずれだけ**を決定的に吸収し、
それを超えるものは推測せずに ``StoryboardSchemaViolationError``（retryable, ADR-0014）にする。
並べ替えはしない（順序は生成器の出力をそのまま信じるか、拒否するか）。
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import replace
from typing import Any

from contracts.artifacts import STORYBOARD_MAX_SCENE_DURATION_MS, STORYBOARD_MIN_SCENE_DURATION_MS
from domain.errors import StoryboardSchemaViolationError
from domain.storyboard.ports import StoryboardSceneDraft

#: 隣接シーン間の隙間・重なりをつなげてよい上限。先頭の開始時刻にも使う。
MAX_GAP_SNAP_MS = 500
#: 最後のシーンの終了を台本の総尺へ合わせてよい上限（±）。
MAX_END_SNAP_MS = 1_000


def normalize_timeline(
    drafts: Sequence[StoryboardSceneDraft], script_total_ms: int
) -> tuple[StoryboardSceneDraft, ...]:
    """隙間なく連続し、0 から ``script_total_ms`` までを覆う下書き列を返す。

    規則（各シーンの**元の終了時刻を保ち**、開始時刻だけを寄せる。誤差が累積しない）:

    1. 開始時刻が前のシーンより前なら拒否（並べ替えない）
    2. 先頭の開始が ``MAX_GAP_SNAP_MS`` 以内なら 0 に寄せる
    3. 前のシーンの終了との隙間・重なりが ``MAX_GAP_SNAP_MS`` 以内なら連続に寄せる
    4. 最後の終了が総尺の ±``MAX_END_SNAP_MS`` 以内なら総尺に寄せる
    5. 寄せた後の尺が契約の範囲外なら拒否
    """
    if not drafts:
        raise StoryboardSchemaViolationError("storyboard has no scenes")

    result: list[StoryboardSceneDraft] = []
    previous_start: int | None = None
    previous_end = 0
    for index, draft in enumerate(drafts):
        if draft.start_ms < 0 or draft.duration_ms <= 0:
            raise StoryboardSchemaViolationError(
                f"scene {index + 1}: negative start or non-positive duration"
            )
        if previous_start is not None and draft.start_ms < previous_start:
            raise StoryboardSchemaViolationError(
                f"scene {index + 1}: scenes are not ordered by start time"
            )
        end = draft.start_ms + draft.duration_ms
        gap = draft.start_ms - previous_end
        if abs(gap) > MAX_GAP_SNAP_MS:
            raise StoryboardSchemaViolationError(
                f"scene {index + 1}: gap/overlap of {gap} ms exceeds {MAX_GAP_SNAP_MS} ms"
            )
        previous_start = draft.start_ms
        start = previous_end
        result.append(replace(draft, start_ms=start, duration_ms=end - start))
        previous_end = end

    drift = previous_end - script_total_ms
    if abs(drift) > MAX_END_SNAP_MS:
        raise StoryboardSchemaViolationError(
            f"storyboard ends at {previous_end} ms but script total is {script_total_ms} ms"
        )
    last = result[-1]
    result[-1] = replace(last, duration_ms=script_total_ms - last.start_ms)

    for index, draft in enumerate(result):
        if not (
            STORYBOARD_MIN_SCENE_DURATION_MS
            <= draft.duration_ms
            <= STORYBOARD_MAX_SCENE_DURATION_MS
        ):
            raise StoryboardSchemaViolationError(
                f"scene {index + 1}: duration {draft.duration_ms} ms out of range "
                f"{STORYBOARD_MIN_SCENE_DURATION_MS}..{STORYBOARD_MAX_SCENE_DURATION_MS}"
            )
    return tuple(result)


def assign_scene_identity(drafts: Sequence[StoryboardSceneDraft]) -> list[dict[str, Any]]:
    """正規化済み下書きに ``order`` / ``scene_id`` を採番し、``StoryboardScene`` の形にする。

    ``start_ms`` は下書きの値をそのまま使う（``normalize_timeline`` 済みであること）。
    戻り値は ``contracts.artifacts.build_storyboard_artifact(scenes=...)`` へ渡す。
    """
    return [
        {
            "scene_id": f"sb{order}",
            "order": order,
            "script_scene_id": draft.script_scene_id,
            "start_ms": draft.start_ms,
            "duration_ms": draft.duration_ms,
            "visual_kind": draft.visual_kind.value,
            "visual_description": draft.visual_description,
            "framing": draft.framing,
            "camera_movement": draft.camera_movement,
            "transition_in": draft.transition_in,
        }
        for order, draft in enumerate(drafts, start=1)
    ]
