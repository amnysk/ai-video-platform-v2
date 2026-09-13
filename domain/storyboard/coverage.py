"""storyboard と台本の突き合わせ（純粋関数、ADR-0015）。

``StoryboardArtifact`` 単体では台本を知らないので検査できない条件をここに置く。
"""

from __future__ import annotations

from contracts.artifacts import ScriptArtifact, StoryboardArtifact
from domain.errors import StoryboardSchemaViolationError


def check_storyboard_covers_script(storyboard: StoryboardArtifact, script: ScriptArtifact) -> None:
    """storyboard が台本を過不足なく覆うことを検査する。違反は ``StoryboardSchemaViolationError``。

    - storyboard の ``script_scene_id`` はすべて台本に存在する
    - 台本の全シーンが1回以上現れる
    - ``script_scene_id`` は台本のシーン順に非減少（前のシーンへ戻らない）
    - ``total_duration_ms`` が台本の総尺と一致する
    """
    position = {scene.id: index for index, scene in enumerate(script.scenes)}

    last_position = -1
    seen: set[str] = set()
    for scene in storyboard.scenes:
        current = position.get(scene.script_scene_id)
        if current is None:
            raise StoryboardSchemaViolationError(
                f"{scene.scene_id} references unknown script scene {scene.script_scene_id}"
            )
        if current < last_position:
            raise StoryboardSchemaViolationError(
                f"{scene.scene_id} goes back to script scene {scene.script_scene_id}"
            )
        last_position = current
        seen.add(scene.script_scene_id)

    missing = [scene.id for scene in script.scenes if scene.id not in seen]
    if missing:
        raise StoryboardSchemaViolationError(f"script scenes not covered by storyboard: {missing}")

    if storyboard.total_duration_ms != script.total_duration_ms:
        raise StoryboardSchemaViolationError(
            f"storyboard total {storyboard.total_duration_ms} ms != "
            f"script total {script.total_duration_ms} ms"
        )
