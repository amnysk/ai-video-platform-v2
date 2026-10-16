"""ADR-0035 (1): Storyboard の時点で各シーンの映像対象を決める規則（domain）と、
その映像対象に応じた画像・動画プロンプトの構図指示。"""

from __future__ import annotations

import pytest

from contracts.artifacts import StoryboardScene, StoryboardVisualKind, VisualSubject
from domain.errors import StoryboardSchemaViolationError
from domain.production.prompting import (
    DEFAULT_IMAGE_STYLE,
    DEFAULT_VIDEO_MOTION,
    build_image_prompt,
    build_video_prompt,
)
from domain.storyboard.visual_subject import (
    PEOPLE_SUBJECTS,
    check_subject_composition,
    subject_from_required_assets,
)

# 2026-09-29 時点（visual_subject 導入前）の文面。旧 storyboard（visual_subject=None）の
# シーンは、この文面のまま画像・動画を依頼し続ける（文面を変えると古い Episode の意味が変わる）。
LEGACY_IMAGE_PROMPT = (
    "clay pot over fire. documentary b-roll shot. framing: close-up. cinematic editorial "
    "illustration, painterly brushwork, dramatic natural lighting, rich detail, vertical 9:16 "
    "composition with the subject centered, stylized artwork rather than a photographic portrait. "
    "no text, no letters, no captions, no watermark, no logo, stylized illustration rather than a "
    "photorealistic likeness of any real or historical person's face."
)
LEGACY_VIDEO_PROMPT = (
    "clay pot over fire. camera: slow push in. opening: fade. subtle natural motion, stable "
    "framing, no cuts, keep the composition of the image. no text, no letters, no captions, no "
    "watermark, no logo, stylized illustration rather than a photorealistic likeness of any real "
    "or historical person's face."
)


def _scene(**overrides) -> StoryboardScene:
    fields = dict(
        scene_id="sb1",
        order=1,
        script_scene_id="s1",
        start_ms=0,
        duration_ms=4000,
        visual_kind=StoryboardVisualKind.BROLL,
        visual_description="clay pot over fire",
        framing="close-up",
        camera_movement="slow push in",
        transition_in="fade",
    )
    fields.update(overrides)
    return StoryboardScene(**fields)  # type: ignore[arg-type]


def _asset(subject: str, source: str = "generate") -> dict[str, str]:
    return {"type": subject, "description": "x", "source": source}


# ---------------------------------------------------------------- required_assets → 映像対象


@pytest.mark.parametrize("subject", list(VisualSubject))
def test_the_single_generate_asset_names_the_visual_subject(subject: VisualSubject) -> None:
    assert subject_from_required_assets([_asset(subject.value)], label="scene 1") is subject


def test_assets_from_other_sources_do_not_count() -> None:
    assets = [_asset("provided_logo", source="provided"), _asset("map")]
    assert subject_from_required_assets(assets, label="scene 1") is VisualSubject.MAP


@pytest.mark.parametrize(
    "assets",
    [None, [], [_asset("map", source="provided")], "map", [_asset("map"), _asset("site")]],
    ids=["missing", "empty", "no-generate", "not-a-list", "two-generate"],
)
def test_the_subject_must_be_decided_exactly_once(assets: object) -> None:
    with pytest.raises(StoryboardSchemaViolationError, match="scene 3: required_assets"):
        subject_from_required_assets(assets, label="scene 3")


def test_a_subject_outside_the_vocabulary_is_a_violation() -> None:
    with pytest.raises(StoryboardSchemaViolationError, match="portrait_of_ieyasu"):
        subject_from_required_assets([_asset("portrait_of_ieyasu")], label="scene 1")


# ---------------------------------------------------------------- 人物を主題にした肖像を作らない


def test_people_subjects_are_the_named_and_the_anonymous_figure() -> None:
    expected = frozenset({VisualSubject.NAMED_PERSON, VisualSubject.FIGURE_ANONYMOUS})
    assert expected == PEOPLE_SUBJECTS


@pytest.mark.parametrize("subject", sorted(PEOPLE_SUBJECTS))
@pytest.mark.parametrize(
    "kind", [StoryboardVisualKind.TALKING_HEAD, StoryboardVisualKind.CHARACTER]
)
def test_a_person_cannot_be_planned_as_a_portrait_shot(
    subject: VisualSubject, kind: StoryboardVisualKind
) -> None:
    with pytest.raises(StoryboardSchemaViolationError, match="scene 2"):
        check_subject_composition(kind, subject, label="scene 2")


@pytest.mark.parametrize(
    ("kind", "subject"),
    [
        # 人物は一律に禁止しない: 遠景・背後の場面としては選べる
        (StoryboardVisualKind.BROLL, VisualSubject.NAMED_PERSON),
        (StoryboardVisualKind.GENERATED, VisualSubject.FIGURE_ANONYMOUS),
        # 人物でない対象は構図の種類を問わない
        (StoryboardVisualKind.CHARACTER, VisualSubject.ARTIFACT),
        (StoryboardVisualKind.TALKING_HEAD, VisualSubject.TEXT_CARD),
    ],
)
def test_other_combinations_are_allowed(kind: StoryboardVisualKind, subject: VisualSubject) -> None:
    check_subject_composition(kind, subject, label="scene 1")


# ---------------------------------------------------------------- プロンプトの構図指示


def test_scenes_without_a_subject_keep_the_legacy_prompt_text() -> None:
    assert build_image_prompt(_scene()) == LEGACY_IMAGE_PROMPT
    assert build_video_prompt(_scene()) == LEGACY_VIDEO_PROMPT


@pytest.mark.parametrize("subject", list(VisualSubject))
def test_every_subject_adds_a_composition_instruction_to_the_image(subject: VisualSubject) -> None:
    prompt = build_image_prompt(_scene(visual_subject=subject))
    assert prompt != LEGACY_IMAGE_PROMPT
    assert prompt.startswith("clay pot over fire.")


@pytest.mark.parametrize("subject", sorted(PEOPLE_SUBJECTS))
def test_people_are_drawn_small_and_not_as_a_recognizable_portrait(
    subject: VisualSubject,
) -> None:
    scene = _scene(
        visual_subject=subject,
        visual_description="Ieyasu watches the battle from a low ridge",
    )
    image = build_image_prompt(scene)
    video = build_video_prompt(scene)
    for prompt in (image, video):
        assert "small in the frame" in prompt
        assert "not a recognizable portrait" in prompt
        # 判定のすり抜けをしない: 人物の名前・史実の記述は消さず、描き方（構図）を変える
        assert "Ieyasu" in prompt


def test_non_people_subjects_do_not_add_people_instructions_to_the_video() -> None:
    video = build_video_prompt(_scene(visual_subject=VisualSubject.MAP))
    assert video == LEGACY_VIDEO_PROMPT


@pytest.mark.parametrize("subject", [VisualSubject.DOCUMENT, VisualSubject.TEXT_CARD])
def test_writing_is_never_baked_into_the_image(subject: VisualSubject) -> None:
    """dedcf315 sb3 には "sohei" の文字が焼き込まれていた。文書でも読める文字を描かせない。"""
    prompt = build_image_prompt(_scene(visual_subject=subject))
    assert "no text, no letters" in prompt
    assert "legible" in prompt


def test_builder_versions_were_raised_for_the_subject_aware_rules() -> None:
    assert DEFAULT_IMAGE_STYLE.style_profile_id.endswith(":prompt-v3")
    assert DEFAULT_VIDEO_MOTION.motion_profile_id.endswith(":video-prompt-v3")
