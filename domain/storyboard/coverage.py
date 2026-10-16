"""storyboard と台本の突き合わせ（純粋関数、ADR-0015）。

``StoryboardArtifact`` 単体では台本を知らないので検査できない条件をここに置く。
"""

from __future__ import annotations

from contracts.artifacts import ScriptArtifact, StoryboardArtifact
from contracts.topic_planning import ScriptLocale
from domain.errors import StoryboardNarrationSpanTooShortError, StoryboardSchemaViolationError


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


def script_scene_spans(storyboard: StoryboardArtifact, script: ScriptArtifact) -> dict[str, int]:
    """台本シーンごとに、描画がその音声に使える区間の長さ（ms）。

    描画（``domain.render.timeline.place_voices``）と同じ置き方: 台本シーンの音声は
    その**最初の** storyboard シーンの開始に置かれ、次の台本シーンの音声の開始（= 次の台本
    シーンの最初の storyboard シーンの開始）までに終わらなければならない。最後の台本シーンは
    storyboard の終端まで（描画の freeze による延長は余裕として数えない）。
    ``check_storyboard_covers_script`` を通った storyboard を前提にする。
    """
    first_start: dict[str, int] = {}
    for scene in storyboard.scenes:
        first_start.setdefault(scene.script_scene_id, scene.start_ms)
    end = max(scene.start_ms + scene.duration_ms for scene in storyboard.scenes)
    starts = [first_start[scene.id] for scene in script.scenes]
    ends = [*starts[1:], end]
    return {
        scene.id: stop - start
        for scene, start, stop in zip(script.scenes, starts, ends, strict=True)
    }


def check_storyboard_fits_narration(
    storyboard: StoryboardArtifact, script: ScriptArtifact, locale: ScriptLocale
) -> None:
    """各台本シーンの区間がナレーションの読み上げ予算を満たすこと（ADR-0026 追補）。

    判定は台本の予算検査と同じ式（``ScriptLocale.narration_budget``）:
    ``count_speech_units(narration) <= narration_budget(span_ms)``、すなわち
    ``span_ms >= required_speech_ms(narration)``。違反は ``StoryboardNarrationSpanTooShortError``
    （retryable、ADR-0014）。修復しない。
    """
    spans = script_scene_spans(storyboard, script)
    short = []
    for scene in script.scenes:
        units = locale.count_speech_units(scene.narration)
        if units > locale.narration_budget(spans[scene.id]):
            short.append(
                f"{scene.id}: span {spans[scene.id]} ms < "
                f"{locale.required_speech_ms(scene.narration)} ms for {units} "
                f"{locale.speech_unit.value}s"
            )
    if short:
        raise StoryboardNarrationSpanTooShortError(
            f"storyboard span too short for narration at {locale.max_speech_units_per_second:g} "
            f"{locale.speech_unit.value}s/s ({locale.locale}): " + "; ".join(short)
        )
