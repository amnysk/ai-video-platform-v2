"""シーン静止画の生成プロンプト（ADR-0017）。純粋関数のみ。provider 中立。

プロンプトの文面は ``input_hash`` に直接入れない。代わりに ``style_profile_id`` が
「どの版の組み立て規則とスタイルか」を表す。**文面を変えたら版を上げる**こと
（上げ忘れると古い画像が新しい規則の結果として再利用される）。
"""

from __future__ import annotations

from dataclasses import dataclass

from contracts.artifacts import StoryboardScene, StoryboardVisualKind

#: 組み立て規則の版。文面・並び・kind の対応を変えたら上げる。
IMAGE_PROMPT_BUILDER_VERSION = "1"


@dataclass(frozen=True, slots=True)
class ImageStyleProfile:
    name: str
    style: str

    @property
    def style_profile_id(self) -> str:
        """``input_hash`` に入る同一性。スタイル名と組み立て規則の版を含む。"""
        return f"{self.name}:prompt-v{IMAGE_PROMPT_BUILDER_VERSION}"


DEFAULT_IMAGE_STYLE = ImageStyleProfile(
    name="vertical-short-cinematic-v1",
    style=(
        "cinematic still frame, natural lighting, rich detail, "
        "vertical 9:16 composition with the subject centered"
    ),
)

_KIND_HINTS: dict[StoryboardVisualKind, str] = {
    StoryboardVisualKind.TALKING_HEAD: "portrait of a presenter facing the camera",
    StoryboardVisualKind.BROLL: "documentary b-roll shot",
    StoryboardVisualKind.ANIMATION: "stylized illustrated animation frame",
    StoryboardVisualKind.CHARACTER: "character-focused shot",
    StoryboardVisualKind.DIAGRAM: "clear explanatory illustration with simple shapes",
    StoryboardVisualKind.TEXT_CARD: "clean uncluttered background with open space for a caption",
    StoryboardVisualKind.TRANSITION: "atmospheric abstract establishing shot",
    StoryboardVisualKind.GENERATED: "high quality generated image",
    StoryboardVisualKind.SCREEN_RECORDING: "illustration of a device screen",
}

#: 文字は後工程で重ねる。生成画像に文字・透かしを焼き込ませない。
_CONSTRAINTS = "no text, no letters, no captions, no watermark, no logo"


def build_image_prompt(
    scene: StoryboardScene, style: ImageStyleProfile = DEFAULT_IMAGE_STYLE
) -> str:
    """storyboard シーン → 静止画プロンプト。動きの指示（camera_movement 等）は含めない。"""
    parts = [scene.visual_description.strip(), _KIND_HINTS[scene.visual_kind]]
    if scene.framing:
        parts.append(f"framing: {scene.framing.strip()}")
    parts.append(style.style)
    parts.append(_CONSTRAINTS)
    return ". ".join(p for p in parts if p) + "."


__all__ = [
    "DEFAULT_IMAGE_STYLE",
    "IMAGE_PROMPT_BUILDER_VERSION",
    "ImageStyleProfile",
    "build_image_prompt",
]
