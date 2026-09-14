"""Upload の契約（ADR-0020）: メタデータ上限・private 固定・マーカー・受領に secret が無い。"""

from __future__ import annotations

import re
import typing
import uuid

import pytest
from pydantic import BaseModel, ValidationError

from contracts.artifacts import ARTIFACT_MODELS, parse_artifact
from contracts.render import FINAL_VIDEO_MAX_BYTES
from contracts.states import (
    UPLOAD_ADMISSIBLE_STATUSES,
    UPLOAD_MEDIA_TASK_QUEUE,
    UPLOAD_TASK_QUEUE,
    UPLOAD_WORKFLOW,
    ArtifactType,
    EpisodeStatus,
    JobType,
    ProviderCall,
)
from contracts.upload import (
    DEFAULT_UPLOAD_CHUNK_BYTES,
    UPLOAD_MARKER_PATTERN,
    YOUTUBE_CHUNK_ALIGNMENT_BYTES,
    YOUTUBE_DESCRIPTION_MAX_BYTES,
    YOUTUBE_TAGS_MAX_TOTAL_CHARS,
    YOUTUBE_TITLE_MAX_CHARS,
    UploadReceiptArtifact,
    YouTubeVideoMetadata,
    build_upload_receipt,
    build_youtube_metadata,
    upload_marker,
    upload_timeout_seconds,
    youtube_tags_length,
)
from contracts.upload_activities import UPLOAD_ACTIVITY_NAMES, UploadFinalVideoResult
from domain.episode.transitions import EpisodeEvent, transition_episode
from domain.upload.keys import compute_upload_key

KEY = "ab" * 32
CHANNEL = "UC" + "a" * 22


def _metadata(**overrides: object) -> dict[str, object]:
    base: dict[str, object] = {
        "title": "t",
        "description": "d",
        "tags": (upload_marker(KEY),),
        "category_id": "22",
        "default_language": "ja",
        "privacy_status": "private",
        "self_declared_made_for_kids": False,
        "contains_synthetic_media": True,
        "notify_subscribers": False,
    }
    return {**base, **overrides}


def _receipt(**overrides: object) -> dict[str, object]:
    return {
        **build_upload_receipt(
            episode_id="ep1",
            source_final_video={
                "artifact_id": str(uuid.uuid4()),
                "sha256": "c" * 64,
                "schema_version": "1.0",
            },
            destination={"platform": "youtube", "channel_id": CHANNEL},
            video_id="abcdefghijk",
            metadata=_metadata(),
            upload_key=KEY,
            bytes=1234,
            reconciled_by="upload_response",
        ),
        **overrides,
    }


# ------------------------------------------------------------------- 語彙


def test_upload_vocabulary_is_declared_once() -> None:
    assert JobType.UPLOAD_FINAL_VIDEO == "upload_final_video"
    assert ArtifactType.UPLOAD_RECEIPT == "upload_receipt"
    assert ProviderCall.YOUTUBE_UPLOAD == "youtube_upload"
    assert UPLOAD_WORKFLOW == ("UploadWorkflow", "upload")
    assert UPLOAD_TASK_QUEUE == "upload"
    assert UPLOAD_MEDIA_TASK_QUEUE == "upload-media"
    assert {
        EpisodeStatus.RENDER_READY,
        EpisodeStatus.NEEDS_WORK,
        EpisodeStatus.BLOCKED,
    } == UPLOAD_ADMISSIBLE_STATUSES
    assert EpisodeStatus.UPLOADED not in UPLOAD_ADMISSIBLE_STATUSES
    assert len(set(UPLOAD_ACTIVITY_NAMES)) == 4


def test_upload_success_moves_in_progress_to_uploaded() -> None:
    assert (
        transition_episode(EpisodeStatus.IN_PROGRESS, EpisodeEvent.UPLOAD_SUCCEEDED)
        == EpisodeStatus.UPLOADED
    )
    # uploaded からは再投稿の入場ができない（409 の根拠）
    assert not isinstance(
        transition_episode(EpisodeStatus.UPLOADED, EpisodeEvent.STAGE_ADMITTED), EpisodeStatus
    )


def test_receipt_is_registered_for_dispatch() -> None:
    assert ARTIFACT_MODELS[ArtifactType.UPLOAD_RECEIPT] is UploadReceiptArtifact
    assert isinstance(parse_artifact(_receipt()), UploadReceiptArtifact)


# ------------------------------------------------------------------- メタデータ


def test_title_limits() -> None:
    YouTubeVideoMetadata.model_validate(_metadata(title="あ" * YOUTUBE_TITLE_MAX_CHARS))
    for bad in ("あ" * (YOUTUBE_TITLE_MAX_CHARS + 1), "a<b", "a>b", "   ", ""):
        with pytest.raises(ValidationError):
            YouTubeVideoMetadata.model_validate(_metadata(title=bad))


def test_description_is_limited_in_utf8_bytes_not_characters() -> None:
    fits = "あ" * (YOUTUBE_DESCRIPTION_MAX_BYTES // 3)
    YouTubeVideoMetadata.model_validate(_metadata(description=fits))
    with pytest.raises(ValidationError):
        YouTubeVideoMetadata.model_validate(_metadata(description=fits + "abc"))
    with pytest.raises(ValidationError):
        YouTubeVideoMetadata.model_validate(_metadata(description="<script>"))


def test_tags_total_counts_quotes_for_tags_with_spaces_and_separators() -> None:
    assert youtube_tags_length(["ab", "c d"]) == 2 + (3 + 2) + 1
    exact = ("a" * 249, "b" * 250)  # 249 + 250 + 1 区切り = 500
    YouTubeVideoMetadata.model_validate(_metadata(tags=exact))
    with pytest.raises(ValidationError):
        YouTubeVideoMetadata.model_validate(_metadata(tags=("a" * 248, "b c" + "b" * 247)))
    assert youtube_tags_length(("a" * 248, "b c" + "b" * 247)) > YOUTUBE_TAGS_MAX_TOTAL_CHARS
    for bad in (("a,b",), ("<x>",), ("x", "x"), (" ",)):
        with pytest.raises(ValidationError):
            YouTubeVideoMetadata.model_validate(_metadata(tags=bad))


@pytest.mark.parametrize("privacy", ["public", "unlisted", "PRIVATE", None])
def test_privacy_status_cannot_be_anything_but_private(privacy: object) -> None:
    """INV-19: public / unlisted はスキーマで表現できない。"""
    with pytest.raises(ValidationError):
        YouTubeVideoMetadata.model_validate(_metadata(privacy_status=privacy))
    with pytest.raises(ValidationError):
        UploadReceiptArtifact.model_validate(_receipt(privacy_status=privacy))


def test_notify_subscribers_is_fixed_false() -> None:
    with pytest.raises(ValidationError):
        YouTubeVideoMetadata.model_validate(_metadata(notify_subscribers=True))


def test_builder_is_deterministic_private_synthetic_and_sanitized() -> None:
    kwargs: dict[str, typing.Any] = {
        "upload_key": KEY,
        "title": " <速報> タイトル ",
        "hook": "フック",
        "narration": "語り" * 3000,
        "language": "ja",
    }
    first = build_youtube_metadata(**kwargs)
    assert first == build_youtube_metadata(**kwargs)
    assert first.privacy_status == "private"
    assert first.contains_synthetic_media is True
    assert first.notify_subscribers is False
    assert first.tags == (upload_marker(KEY),)
    assert "<" not in first.title and ">" not in first.title
    assert len(first.description.encode("utf-8")) <= YOUTUBE_DESCRIPTION_MAX_BYTES


# ------------------------------------------------------------------- マーカー / キー


def test_upload_marker_format() -> None:
    marker = upload_marker(KEY)
    assert re.fullmatch(UPLOAD_MARKER_PATTERN, marker)
    assert marker == "avpu" + KEY[:24]
    assert " " not in marker and youtube_tags_length([marker]) == len(marker)
    for bad in ("AB" * 32, "ab" * 31, "zz" * 32):
        with pytest.raises(ValueError):
            upload_marker(bad)


def test_upload_key_excludes_attempt_and_time_and_depends_on_inputs() -> None:
    key = compute_upload_key(episode_id="e", final_video_sha256="c" * 64, destination_id=CHANNEL)
    assert re.fullmatch(r"[0-9a-f]{64}", key)
    assert key == compute_upload_key(
        episode_id="e", final_video_sha256="c" * 64, destination_id=CHANNEL
    )
    assert key != compute_upload_key(
        episode_id="e", final_video_sha256="d" * 64, destination_id=CHANNEL
    )
    assert key != compute_upload_key(
        episode_id="e", final_video_sha256="c" * 64, destination_id="UC" + "b" * 22
    )


# ------------------------------------------------------------------- 受領


def test_receipt_requires_marker_of_its_own_key() -> None:
    with pytest.raises(ValidationError):
        UploadReceiptArtifact.model_validate(_receipt(upload_key="cd" * 32))


def test_receipt_rejects_bad_ids_and_sizes() -> None:
    for overrides in (
        {"video_id": "short"},
        {"bytes": 0},
        {"bytes": FINAL_VIDEO_MAX_BYTES + 1},
        {"destination": {"platform": "youtube", "channel_id": "nope"}},
        {"destination": {"platform": "tiktok", "channel_id": CHANNEL}},
        {"reconciled_by": "guess"},
        {"uploaded_at": "2026-09-15T00:00:00Z"},
    ):
        with pytest.raises(ValidationError):
            UploadReceiptArtifact.model_validate(_receipt(**overrides))


_SECRET_LIKE = re.compile(
    r"token|secret|session|uri|url|password|credential|refresh|authorization|cookie|location",
    re.IGNORECASE,
)


def _field_names(model: type[BaseModel]) -> set[str]:
    names: set[str] = set()
    for name, field in model.model_fields.items():
        names.add(name)
        for arg in (field.annotation, *typing.get_args(field.annotation)):
            if isinstance(arg, type) and issubclass(arg, BaseModel):
                names |= _field_names(arg)
    return names


def test_receipt_and_activity_result_have_no_secret_like_fields() -> None:
    """INV-20: 受領 Artifact と Activity 結果に secret / session URI の欄が無い。"""
    names = _field_names(UploadReceiptArtifact) | set(UploadFinalVideoResult.__dataclass_fields__)
    assert not [n for n in names if _SECRET_LIKE.search(n)]


def test_receipt_content_has_no_wall_clock_time() -> None:
    """再照合で同じ bytes になるよう、時刻の欄を持たない（ADR-0020 §6）。"""
    assert not [n for n in _field_names(UploadReceiptArtifact) if n.endswith("_at")]


def test_chunk_size_and_timeout_policy() -> None:
    assert DEFAULT_UPLOAD_CHUNK_BYTES % YOUTUBE_CHUNK_ALIGNMENT_BYTES == 0
    assert upload_timeout_seconds(1) >= 15 * 60
    assert upload_timeout_seconds(FINAL_VIDEO_MAX_BYTES) > upload_timeout_seconds(1)
