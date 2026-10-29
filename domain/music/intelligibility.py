"""ナレーションの聞き取りやすさ: 発話中の「声 - BGM」のラウドネス差の評価（ADR-0083 §5、INV-135）。

純粋関数。測るのは infrastructure 側（``infrastructure/render/bgm_intelligibility.py``）で、
ここは測った値（1/100 LUFS の int）を閾値と突き合わせて理由つきの結果にする。

- 差 = 声のラウドネス - BGM のラウドネス（ducking 後・同じ発話区間）。大きいほど声が聞き取りやすい
- BGM が鳴っていない区間（``music_lufs_centi=None``）は差を持たず、不合格にもしない
- 声が測れない区間は差を持たない。**1 つも差が求まらなければ不合格**（測れていないものを通さない）
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass

from contracts.music_render import (
    SPEECH_MARGIN_MIN_CENTI_LU,
    SpeechIntelligibility,
    SpeechMarginScene,
)


@dataclass(frozen=True, slots=True)
class SceneSpeechLevels:
    scene_id: str
    voice_lufs_centi: int | None
    music_lufs_centi: int | None


def evaluate_speech_intelligibility(
    levels: Sequence[SceneSpeechLevels], *, threshold_centi_lu: int = SPEECH_MARGIN_MIN_CENTI_LU
) -> SpeechIntelligibility:
    if not levels:
        raise ValueError("levels must not be empty")
    scenes: list[SpeechMarginScene] = []
    for lv in levels:
        margin = (
            lv.voice_lufs_centi - lv.music_lufs_centi
            if lv.voice_lufs_centi is not None and lv.music_lufs_centi is not None
            else None
        )
        scenes.append(
            SpeechMarginScene(
                scene_id=lv.scene_id,
                voice_lufs_centi=lv.voice_lufs_centi,
                music_lufs_centi=lv.music_lufs_centi,
                margin_centi_lu=margin,
            )
        )
    margins = [s.margin_centi_lu for s in scenes if s.margin_centi_lu is not None]
    # BGM が鳴っていない（声は測れた）区間だけが並ぶ場合は、聞き取りを妨げるものが無いので合格
    silent_music = [
        s for s in scenes if s.voice_lufs_centi is not None and s.music_lufs_centi is None
    ]
    smallest = min(margins) if margins else None
    passed = smallest >= threshold_centi_lu if smallest is not None else bool(silent_music)
    return SpeechIntelligibility(
        scenes=tuple(scenes),
        min_margin_centi_lu=smallest,
        threshold_centi_lu=threshold_centi_lu,
        passed=passed,
    )


__all__ = ["SceneSpeechLevels", "evaluate_speech_intelligibility"]
