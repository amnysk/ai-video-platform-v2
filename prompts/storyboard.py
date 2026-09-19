"""storyboard prompt に埋めるデータの整形（ADR-0026 追補2）。"""

from __future__ import annotations

from contracts.artifacts import ScriptArtifact
from contracts.topic_planning import script_locale_for_language


def storyboard_section_durations(script: ScriptArtifact) -> str:
    """台本シーンごとの尺と、ナレーションに必要な最短の区間（ms）を prompt 用に並べる。

    最短の区間は ``ScriptLocale.required_speech_ms``（locale は台本の ``language`` から引く）。
    """
    locale = script_locale_for_language(script.language)
    return "\n".join(
        f"- {scene.id}: {scene.duration_ms} ms"
        f"（ナレーションに最低 {locale.required_speech_ms(scene.narration)} ms）"
        for scene in script.scenes
    )


__all__ = ["storyboard_section_durations"]
