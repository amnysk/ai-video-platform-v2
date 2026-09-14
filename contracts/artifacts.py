"""Artifactのスキーマ定義（INV-10）。生成側と取り込み側がここを共有する。"""

from __future__ import annotations

import json
import re
import uuid
from enum import StrEnum
from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from contracts.artifact_refs import (
    SCRIPT_SCENE_ID_PATTERN,
    SHA256_HEX_PATTERN,
    STORYBOARD_SCENE_ID_PATTERN,
    ArtifactDigestRef,
    FrozenModel,
    SourceScriptRef,
    SourceStoryboardRef,
    check_canonical_uuid,
)
from contracts.artifact_refs import (
    SourceArtifactRef as SourceArtifactRef,  # 再公開（定義は artifact_refs）
)
from contracts.render import RENDER_ARTIFACT_SCHEMA_VERSION, FinalVideoArtifact
from contracts.states import ArtifactType

DUMMY_ARTIFACT_SCHEMA_VERSION = "1.0"
DUMMY_ARTIFACT_MESSAGE = "workflow completed"

SCRIPT_ARTIFACT_SCHEMA_VERSION = "1.0"
SCRIPT_MIN_SCENES = 3
SCRIPT_MAX_SCENES = 8
SCRIPT_MIN_TOTAL_DURATION_MS = 15_000
SCRIPT_MAX_TOTAL_DURATION_MS = 60_000  # YouTube Shorts の上限

#: シーン1件あたりの尺の範囲（ミリ秒）。
SCRIPT_MIN_SCENE_DURATION_MS = 1_000
SCRIPT_MAX_SCENE_DURATION_MS = 20_000


#: ナレーションを連結するときの区切り。導出値の定義を1箇所に置く。
SCRIPT_NARRATION_JOINER = " "


class DummyArtifact(BaseModel):
    """Phase 1 の骨組み用ダミー成果物。"""

    model_config = ConfigDict(extra="forbid", frozen=True)

    episode_id: str
    type: Literal[ArtifactType.DUMMY]
    schema_version: Literal["1.0"]
    message: str


class ScriptScene(BaseModel):
    """台本の1シーン。

    ``duration_ms`` は **int（ミリ秒）**。秒の float を持たないのは、浮動小数の
    表現差で正準JSONのバイト列が割れ、同じ内容が違う SHA-256 になるのを
    構造的に防ぐため。
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    id: str = Field(pattern=SCRIPT_SCENE_ID_PATTERN)
    narration: str = Field(min_length=1, max_length=220)
    visual: str = Field(min_length=1, max_length=400)
    duration_ms: int = Field(ge=SCRIPT_MIN_SCENE_DURATION_MS, le=SCRIPT_MAX_SCENE_DURATION_MS)


class ScriptMetadata(BaseModel):
    """台本の来歴。どの生成器のどのモデルが書いたかを成果物自身に残す。"""

    model_config = ConfigDict(extra="forbid", frozen=True)

    topic: str = Field(min_length=1, max_length=200)
    generator: str = Field(min_length=1, max_length=64)
    generator_model: str = Field(min_length=1, max_length=64)


class ScriptArtifact(BaseModel):
    """台本成果物。

    トップレベルに ``narration`` / ``total_duration_ms`` の**フィールドを置かない**。
    どちらも ``scenes`` から一意に決まるので、保存すると同じ真実が2箇所に散り、
    片方だけ更新された成果物を作れてしまう（AGENTS.md §8）。導出は property。
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    episode_id: str = Field(min_length=1, max_length=64)
    type: Literal[ArtifactType.SCRIPT]
    schema_version: Literal["1.0"]
    # 既定値を置かない。言語は必ず生成側が宣言する（黙って ja になるのを防ぐ）。
    language: Literal["ja", "en"]
    title: str = Field(min_length=1, max_length=100)
    hook: str = Field(min_length=1, max_length=80)
    scenes: Annotated[
        tuple[ScriptScene, ...],
        Field(min_length=SCRIPT_MIN_SCENES, max_length=SCRIPT_MAX_SCENES),
    ]
    metadata: ScriptMetadata

    @property
    def total_duration_ms(self) -> int:
        """総尺。``scenes`` から導出する（保存しない）。"""
        return sum(scene.duration_ms for scene in self.scenes)

    @property
    def narration(self) -> str:
        """全ナレーション。``scenes[].narration`` から導出する（保存しない）。"""
        return SCRIPT_NARRATION_JOINER.join(scene.narration for scene in self.scenes)

    @model_validator(mode="after")
    def _check_scene_invariants(self) -> ScriptArtifact:
        seen: set[str] = set()
        for scene in self.scenes:
            if scene.id in seen:
                raise ValueError(f"duplicate scene id: {scene.id}")
            seen.add(scene.id)

        total = self.total_duration_ms
        if not (SCRIPT_MIN_TOTAL_DURATION_MS <= total <= SCRIPT_MAX_TOTAL_DURATION_MS):
            raise ValueError(
                "total duration must be within "
                f"{SCRIPT_MIN_TOTAL_DURATION_MS}..{SCRIPT_MAX_TOTAL_DURATION_MS} ms, got {total}"
            )
        return self


STORYBOARD_ARTIFACT_SCHEMA_VERSION = "1.0"
STORYBOARD_MIN_SCENES = 1
STORYBOARD_MAX_SCENES = 24
STORYBOARD_MIN_SCENE_DURATION_MS = 500
STORYBOARD_MAX_SCENE_DURATION_MS = 20_000
#: 総尺の範囲は台本と同じ（storyboard の総尺は台本の総尺に一致しなければならない）。
STORYBOARD_MIN_TOTAL_DURATION_MS = SCRIPT_MIN_TOTAL_DURATION_MS
STORYBOARD_MAX_TOTAL_DURATION_MS = SCRIPT_MAX_TOTAL_DURATION_MS


class StoryboardVisualKind(StrEnum):
    """シーンの映像の種類（ADR-0015）。platform 所有の語彙。

    生成器側の語彙（例: 外部スキーマの scene type）からの変換はアダプタが持つ。
    """

    TALKING_HEAD = "talking_head"
    BROLL = "broll"
    ANIMATION = "animation"
    CHARACTER = "character"
    DIAGRAM = "diagram"
    TEXT_CARD = "text_card"
    TRANSITION = "transition"
    GENERATED = "generated"
    SCREEN_RECORDING = "screen_recording"


class StoryboardSourceScript(BaseModel):
    """storyboard の入力台本の固定（artifact_id / sha256 / schema_version）。"""

    model_config = ConfigDict(extra="forbid", frozen=True)

    artifact_id: str
    sha256: str = Field(pattern=SHA256_HEX_PATTERN)
    schema_version: Literal["1.0"]

    @field_validator("artifact_id")
    @classmethod
    def _canonical_uuid(cls, value: str) -> str:
        if str(uuid.UUID(value)) != value:
            raise ValueError(f"artifact_id must be a canonical UUID: {value!r}")
        return value


class StoryboardScene(BaseModel):
    """storyboard の1シーン。時間は int ミリ秒（ScriptScene と同じ理由）。

    ``scene_id`` / ``order`` / ``start_ms`` はシステムが採番する。生成器に決めさせない。
    ナレーションは持たない。``script_scene_id`` で台本を参照する（単一の真実）。
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    scene_id: str = Field(pattern=STORYBOARD_SCENE_ID_PATTERN)
    order: int = Field(ge=1)
    script_scene_id: str = Field(pattern=SCRIPT_SCENE_ID_PATTERN)
    start_ms: int = Field(ge=0)
    duration_ms: int = Field(
        ge=STORYBOARD_MIN_SCENE_DURATION_MS, le=STORYBOARD_MAX_SCENE_DURATION_MS
    )
    visual_kind: StoryboardVisualKind
    visual_description: str = Field(min_length=1, max_length=600)
    framing: str | None = Field(default=None, max_length=120)
    camera_movement: str | None = Field(default=None, max_length=120)
    transition_in: str | None = Field(default=None, max_length=60)


class StoryboardMetadata(BaseModel):
    """storyboard の来歴。``generation_spec_id`` は不透明な仕様の同一性（ADR-0016）。"""

    model_config = ConfigDict(extra="forbid", frozen=True)

    generator: str = Field(min_length=1, max_length=128)
    generator_model: str = Field(min_length=1, max_length=64)
    generation_spec_id: str = Field(min_length=1, max_length=128)


class StoryboardArtifact(BaseModel):
    """storyboard 成果物（ADR-0015）。外部の scene_plan スキーマとは別の platform 契約。

    ``total_duration_ms`` は保存する（台本の総尺との一致をドメインで検査するため）。
    ただしシーンの尺の合計と一致しなければならない（ここで検査する）。
    台本シーンの順序・カバレッジは台本が要るので ``domain/storyboard/coverage.py`` が検査する。
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    episode_id: str = Field(min_length=1, max_length=64)
    type: Literal[ArtifactType.STORYBOARD]
    schema_version: Literal["1.0"]
    source_script: StoryboardSourceScript
    scenes: Annotated[
        tuple[StoryboardScene, ...],
        Field(min_length=STORYBOARD_MIN_SCENES, max_length=STORYBOARD_MAX_SCENES),
    ]
    total_duration_ms: int = Field(
        ge=STORYBOARD_MIN_TOTAL_DURATION_MS, le=STORYBOARD_MAX_TOTAL_DURATION_MS
    )
    metadata: StoryboardMetadata

    @model_validator(mode="after")
    def _check_timeline(self) -> StoryboardArtifact:
        expected_start = 0
        for index, scene in enumerate(self.scenes, start=1):
            if scene.order != index:
                raise ValueError(
                    f"scene orders must be 1..N consecutive; got {scene.order} at {index}"
                )
            if scene.scene_id != f"sb{index}":
                raise ValueError(f"scene_id must be sb{index}, got {scene.scene_id}")
            if scene.start_ms != expected_start:
                raise ValueError(
                    f"scene {scene.scene_id} must start at {expected_start} ms, "
                    f"got {scene.start_ms}"
                )
            expected_start += scene.duration_ms
        if expected_start != self.total_duration_ms:
            raise ValueError(
                f"sum of scene durations {expected_start} != total_duration_ms "
                f"{self.total_duration_ms}"
            )
        return self


# --------------------------------------------------------------------------- Production（ADR-0017）

PRODUCTION_ARTIFACT_SCHEMA_VERSION = "1.0"
#: メディア1件の上限（バイト）。provider が返す巨大ファイルを Artifact にしない。
MEDIA_MAX_BYTES = 25 * 1024 * 1024
IMAGE_MIME_TYPES: tuple[str, ...] = ("image/png", "image/jpeg", "image/webp")
AUDIO_MIME_TYPES: tuple[str, ...] = ("audio/wav",)
VIDEO_MIME_TYPES: tuple[str, ...] = ("video/mp4",)


class MediaDescriptor(FrozenModel):
    """メディア本体（MinIO 上のバイナリ）の所在と指紋。

    ``object_key`` は ``domain.artifact.keys.media_object_key`` の規約。物理パスは持たない。
    """

    object_key: str = Field(min_length=1, max_length=1024)
    sha256: str = Field(pattern=SHA256_HEX_PATTERN)
    bytes: int = Field(gt=0, le=MEDIA_MAX_BYTES)
    mime: str = Field(min_length=1, max_length=64)


class GeneratorMetadata(FrozenModel):
    """生成の来歴。provider の生レスポンスや provider job id は**持たない**（ADR-0017）。"""

    generator: str = Field(min_length=1, max_length=128)
    generator_model: str = Field(min_length=1, max_length=128)
    generation_profile_id: str = Field(min_length=1, max_length=128)


def _require_mime(media: MediaDescriptor, allowed: tuple[str, ...]) -> None:
    if media.mime not in allowed:
        raise ValueError(f"media mime {media.mime!r} not in {allowed}")


class SceneImageArtifact(FrozenModel):
    """storyboard シーン1件の静止画（ADR-0017）。"""

    episode_id: str = Field(min_length=1, max_length=64)
    type: Literal[ArtifactType.SCENE_IMAGE]
    schema_version: Literal["1.0"]
    source_storyboard: SourceStoryboardRef
    scene_id: str = Field(pattern=STORYBOARD_SCENE_ID_PATTERN)
    media: MediaDescriptor
    width: int = Field(gt=0)
    height: int = Field(gt=0)
    generator: GeneratorMetadata

    @model_validator(mode="after")
    def _check_mime(self) -> SceneImageArtifact:
        _require_mime(self.media, IMAGE_MIME_TYPES)
        return self


class SceneVoiceArtifact(FrozenModel):
    """台本シーン1件のナレーション音声。ナレーション文は複製しない（台本が単一の真実）。"""

    episode_id: str = Field(min_length=1, max_length=64)
    type: Literal[ArtifactType.SCENE_VOICE]
    schema_version: Literal["1.0"]
    source_storyboard: SourceStoryboardRef
    source_script: SourceScriptRef
    script_scene_id: str = Field(pattern=SCRIPT_SCENE_ID_PATTERN)
    storyboard_scene_ids: Annotated[
        tuple[Annotated[str, Field(pattern=STORYBOARD_SCENE_ID_PATTERN)], ...],
        Field(min_length=1),
    ]
    language: Literal["ja", "en"]
    voice_id: str = Field(min_length=1, max_length=128)
    media: MediaDescriptor
    duration_ms: int = Field(gt=0)
    sample_rate_hz: int = Field(gt=0)
    channels: int = Field(ge=1, le=2)
    generator: GeneratorMetadata

    @model_validator(mode="after")
    def _check(self) -> SceneVoiceArtifact:
        _require_mime(self.media, AUDIO_MIME_TYPES)
        if len(set(self.storyboard_scene_ids)) != len(self.storyboard_scene_ids):
            raise ValueError("storyboard_scene_ids must be unique")
        return self


class SceneVideoArtifact(FrozenModel):
    """storyboard シーン1件の動画（静止画から生成。音声は持たない）。

    fps は ``fps_millis``（fps × 1000 の int）で持つ。float は正準JSONの sha256 を揺らす。
    """

    episode_id: str = Field(min_length=1, max_length=64)
    type: Literal[ArtifactType.SCENE_VIDEO]
    schema_version: Literal["1.0"]
    source_storyboard: SourceStoryboardRef
    scene_id: str = Field(pattern=STORYBOARD_SCENE_ID_PATTERN)
    source_image: ArtifactDigestRef
    media: MediaDescriptor
    duration_ms: int = Field(gt=0)
    requested_duration_ms: int = Field(gt=0)
    width: int = Field(gt=0)
    height: int = Field(gt=0)
    fps_millis: int = Field(gt=0)
    has_audio: Literal[False]
    generator: GeneratorMetadata

    @model_validator(mode="after")
    def _check_mime(self) -> SceneVideoArtifact:
        _require_mime(self.media, VIDEO_MIME_TYPES)
        return self


class ManifestScene(FrozenModel):
    scene_id: str = Field(pattern=STORYBOARD_SCENE_ID_PATTERN)
    image: ArtifactDigestRef
    video: ArtifactDigestRef


class ManifestVoice(FrozenModel):
    script_scene_id: str = Field(pattern=SCRIPT_SCENE_ID_PATTERN)
    artifact_id: str
    sha256: str = Field(pattern=SHA256_HEX_PATTERN)

    @field_validator("artifact_id")
    @classmethod
    def _canonical_uuid(cls, value: str) -> str:
        return check_canonical_uuid(value)


class ProductionManifest(FrozenModel):
    """1 Episode の production 成果の一覧（ADR-0017）。

    storyboard / 台本に対するカバレッジ（全シーンに画像+動画、全台本シーンに音声）は
    両者が要るので ``domain/production/manifest.py`` が検査する。ここでは重複だけを止める。
    """

    episode_id: str = Field(min_length=1, max_length=64)
    type: Literal[ArtifactType.PRODUCTION_MANIFEST]
    schema_version: Literal["1.0"]
    source_storyboard: SourceStoryboardRef
    source_script: SourceScriptRef
    scenes: Annotated[tuple[ManifestScene, ...], Field(min_length=1)]
    voices: Annotated[tuple[ManifestVoice, ...], Field(min_length=1)]

    @model_validator(mode="after")
    def _unique(self) -> ProductionManifest:
        scene_ids = [s.scene_id for s in self.scenes]
        if len(set(scene_ids)) != len(scene_ids):
            raise ValueError("duplicate scene_id in manifest")
        voice_ids = [v.script_scene_id for v in self.voices]
        if len(set(voice_ids)) != len(voice_ids):
            raise ValueError("duplicate script_scene_id in manifest")
        return self


def _build(model: type[BaseModel], artifact_type: ArtifactType, fields: dict[str, Any]) -> dict:
    artifact = model.model_validate(
        {
            **fields,
            "type": artifact_type.value,
            "schema_version": PRODUCTION_ARTIFACT_SCHEMA_VERSION,
        }
    )
    return artifact.model_dump(mode="json")


def build_scene_image_artifact(
    *,
    episode_id: str,
    source_storyboard: Any,
    scene_id: str,
    media: Any,
    width: int,
    height: int,
    generator: Any,
) -> dict[str, Any]:
    """生成側。build の時点で検証を通してから dict を返す。"""
    return _build(
        SceneImageArtifact,
        ArtifactType.SCENE_IMAGE,
        {
            "episode_id": episode_id,
            "source_storyboard": source_storyboard,
            "scene_id": scene_id,
            "media": media,
            "width": width,
            "height": height,
            "generator": generator,
        },
    )


def parse_scene_image_artifact(payload: dict[str, Any]) -> SceneImageArtifact:
    return SceneImageArtifact.model_validate(payload)


def build_scene_voice_artifact(
    *,
    episode_id: str,
    source_storyboard: Any,
    source_script: Any,
    script_scene_id: str,
    storyboard_scene_ids: Any,
    language: str,
    voice_id: str,
    media: Any,
    duration_ms: int,
    sample_rate_hz: int,
    channels: int,
    generator: Any,
) -> dict[str, Any]:
    return _build(
        SceneVoiceArtifact,
        ArtifactType.SCENE_VOICE,
        {
            "episode_id": episode_id,
            "source_storyboard": source_storyboard,
            "source_script": source_script,
            "script_scene_id": script_scene_id,
            "storyboard_scene_ids": storyboard_scene_ids,
            "language": language,
            "voice_id": voice_id,
            "media": media,
            "duration_ms": duration_ms,
            "sample_rate_hz": sample_rate_hz,
            "channels": channels,
            "generator": generator,
        },
    )


def parse_scene_voice_artifact(payload: dict[str, Any]) -> SceneVoiceArtifact:
    return SceneVoiceArtifact.model_validate(payload)


def build_scene_video_artifact(
    *,
    episode_id: str,
    source_storyboard: Any,
    scene_id: str,
    source_image: Any,
    media: Any,
    duration_ms: int,
    requested_duration_ms: int,
    width: int,
    height: int,
    fps_millis: int,
    generator: Any,
) -> dict[str, Any]:
    return _build(
        SceneVideoArtifact,
        ArtifactType.SCENE_VIDEO,
        {
            "episode_id": episode_id,
            "source_storyboard": source_storyboard,
            "scene_id": scene_id,
            "source_image": source_image,
            "media": media,
            "duration_ms": duration_ms,
            "requested_duration_ms": requested_duration_ms,
            "width": width,
            "height": height,
            "fps_millis": fps_millis,
            "has_audio": False,
            "generator": generator,
        },
    )


def parse_scene_video_artifact(payload: dict[str, Any]) -> SceneVideoArtifact:
    return SceneVideoArtifact.model_validate(payload)


def build_production_manifest(
    *,
    episode_id: str,
    source_storyboard: Any,
    source_script: Any,
    scenes: Any,
    voices: Any,
) -> dict[str, Any]:
    """生成側。カバレッジ検査は ``domain.production.manifest.build_manifest`` が行う。"""
    return _build(
        ProductionManifest,
        ArtifactType.PRODUCTION_MANIFEST,
        {
            "episode_id": episode_id,
            "source_storyboard": source_storyboard,
            "source_script": source_script,
            "scenes": scenes,
            "voices": voices,
        },
    )


def parse_production_manifest(payload: dict[str, Any]) -> ProductionManifest:
    return ProductionManifest.model_validate(payload)


def build_final_video_artifact(
    *,
    episode_id: str,
    source_production_manifest: Any,
    source_script: Any,
    source_storyboard: Any,
    render_profile: Any,
    render_policy: Any,
    render_plan_sha256: str,
    render_engine: Any,
    template_version: int,
    media: Any,
    measured: Any,
    total_duration_ms: int,
    timeline: Any,
    voice_placements: Any,
    subtitle_cues: Any,
    technical_qa: Any,
) -> dict[str, Any]:
    """生成側（ADR-0019）。build の時点で検証（時間軸・実測・技術検査の合格）を通してから返す。"""
    artifact = FinalVideoArtifact.model_validate(
        {
            "episode_id": episode_id,
            "type": ArtifactType.FINAL_VIDEO.value,
            "schema_version": RENDER_ARTIFACT_SCHEMA_VERSION,
            "source_production_manifest": source_production_manifest,
            "source_script": source_script,
            "source_storyboard": source_storyboard,
            "render_profile": render_profile,
            "render_policy": render_policy,
            "render_plan_sha256": render_plan_sha256,
            "render_engine": render_engine,
            "template_version": template_version,
            "media": media,
            "measured": measured,
            "total_duration_ms": total_duration_ms,
            "timeline": timeline,
            "voice_placements": voice_placements,
            "subtitle_cues": subtitle_cues,
            "technical_qa": technical_qa,
        }
    )
    return artifact.model_dump(mode="json")


def parse_final_video(payload: dict[str, Any]) -> FinalVideoArtifact:
    """取り込み側。想定外の schema_version は推測せず ValidationError にする。"""
    return FinalVideoArtifact.model_validate(payload)


#: ArtifactType -> モデル。``parse_artifact`` のディスパッチ表。
#: 新しい ArtifactType を足したらここにも登録する
#: （``test_every_artifact_type_has_a_registered_model`` が強制する）。
ARTIFACT_MODELS: dict[ArtifactType, type[BaseModel]] = {
    ArtifactType.DUMMY: DummyArtifact,
    ArtifactType.SCRIPT: ScriptArtifact,
    ArtifactType.STORYBOARD: StoryboardArtifact,
    ArtifactType.SCENE_IMAGE: SceneImageArtifact,
    ArtifactType.SCENE_VOICE: SceneVoiceArtifact,
    ArtifactType.SCENE_VIDEO: SceneVideoArtifact,
    ArtifactType.PRODUCTION_MANIFEST: ProductionManifest,
    ArtifactType.FINAL_VIDEO: FinalVideoArtifact,
}


def build_dummy_artifact(*, episode_id: str) -> dict[str, Any]:
    """生成側。ここが返す形だけが正。"""
    return {
        "episode_id": episode_id,
        "type": ArtifactType.DUMMY.value,
        "schema_version": DUMMY_ARTIFACT_SCHEMA_VERSION,
        "message": DUMMY_ARTIFACT_MESSAGE,
    }


def build_script_artifact(
    *,
    episode_id: str,
    language: str,
    title: str,
    hook: str,
    scenes: Any,
    metadata: Any,
) -> dict[str, Any]:
    """生成側。**build の時点で検証を通してから** dict を返す。

    ``build_dummy_artifact`` は生 dict を返すが、Script は意図的に異なる。
    ダミーは値がこの関数の中で閉じているのに対し、台本の中身は外部生成器
    （LLM）由来で、ここが不正な成果物を作れる唯一の入口になるため。
    """
    artifact = ScriptArtifact.model_validate(
        {
            "episode_id": episode_id,
            "type": ArtifactType.SCRIPT.value,
            "schema_version": SCRIPT_ARTIFACT_SCHEMA_VERSION,
            "language": language,
            "title": title,
            "hook": hook,
            "scenes": scenes,
            "metadata": metadata,
        }
    )
    return artifact.model_dump(mode="json")


def parse_script_artifact(payload: dict[str, Any]) -> ScriptArtifact:
    """取り込み側。想定外の schema_version は推測せず ValidationError にする。"""
    return ScriptArtifact.model_validate(payload)


def build_storyboard_artifact(
    *,
    episode_id: str,
    source_script: Any,
    scenes: Any,
    total_duration_ms: int,
    metadata: Any,
) -> dict[str, Any]:
    """生成側。build の時点で検証を通してから dict を返す。

    理由は ``build_script_artifact`` と同じ（中身が外部生成器由来）。
    """
    artifact = StoryboardArtifact.model_validate(
        {
            "episode_id": episode_id,
            "type": ArtifactType.STORYBOARD.value,
            "schema_version": STORYBOARD_ARTIFACT_SCHEMA_VERSION,
            "source_script": source_script,
            "scenes": scenes,
            "total_duration_ms": total_duration_ms,
            "metadata": metadata,
        }
    )
    return artifact.model_dump(mode="json")


def parse_storyboard_artifact(payload: dict[str, Any]) -> StoryboardArtifact:
    """取り込み側。想定外の schema_version は推測せず ValidationError にする。"""
    return StoryboardArtifact.model_validate(payload)


AnyArtifact = (
    DummyArtifact
    | ScriptArtifact
    | StoryboardArtifact
    | SceneImageArtifact
    | SceneVoiceArtifact
    | SceneVideoArtifact
    | ProductionManifest
    | FinalVideoArtifact
)


def parse_artifact(payload: dict[str, Any]) -> AnyArtifact:
    """取り込み側の唯一の入口。payload の ``type`` でディスパッチする。

    未知の type・type 欠落は推測せず ``ValueError``。「読めそうな型で試す」を
    しないのは、間違った型で通ってしまう成果物を作らないため（INV-10）。
    """
    raw_type = payload.get("type")
    if raw_type is None:
        raise ValueError("artifact payload has no 'type'")
    try:
        artifact_type = ArtifactType(raw_type)
    except ValueError as exc:
        raise ValueError(f"unknown artifact type: {raw_type!r}") from exc
    model = ARTIFACT_MODELS[artifact_type]
    return model.model_validate(payload)  # type: ignore[return-value]


_CODE_FENCE_RE = re.compile(r"^\s*```(?:[A-Za-z0-9_+-]*)\s*\n(?P<body>.*?)\n?\s*```\s*$", re.DOTALL)


def extract_json_object(raw: str) -> dict[str, Any]:
    """生成器の生出力から JSON オブジェクトを取り出す。

    手順は決定的: フェンス除去 → 最初の ``{`` から最後の ``}`` を切り出す →
    ``json.loads``。**修復はしない**（截断JSONの閉じ括弧補完など）。推測して
    読まない（ADR-0014）。

    失敗時は ``ValueError`` を送出する。``contracts/`` は ``domain/`` を import
    できない（INV-6）ので、ここで ``ScriptOutputUnparseableError`` などの
    ドメイン例外へ投げ分けることはできない。**この ValueError をドメイン例外へ
    変換するのは呼び出し側（worker / adapter）の責務**。
    """
    text = raw.strip()
    fenced = _CODE_FENCE_RE.match(text)
    if fenced is not None:
        text = fenced.group("body").strip()

    start = text.find("{")
    end = text.rfind("}")
    if start == -1 or end == -1 or end < start:
        raise ValueError("no JSON object found in generator output")

    try:
        parsed = json.loads(text[start : end + 1])
    except json.JSONDecodeError as exc:
        raise ValueError(f"generator output is not valid JSON: {exc}") from exc

    if not isinstance(parsed, dict):
        raise ValueError(f"expected a JSON object, got {type(parsed).__name__}")
    return parsed
