"""description に内部 ID を出さない・marker は tags に残す・必須部分を黙って切らない（ADR-0070）。

flag（``AVP_UPLOAD_DESCRIPTION_WITHOUT_MARKER``）は既定 OFF。OFF の挙動は従来と byte 単位で同じ。
"""

from __future__ import annotations

import uuid

import pytest

from contracts.upload import (
    UPLOAD_DESCRIPTION_WITHOUT_MARKER_ENV,
    YOUTUBE_DESCRIPTION_MAX_BYTES,
    DescriptionRequiredPartsTooLongError,
    InternalIdInDescriptionError,
    YouTubeVideoMetadata,
    build_youtube_metadata,
    description_without_marker_enabled,
    find_internal_ids,
    upload_marker,
)
from infrastructure.youtube.metadata import insert_body
from tests.support.fake_youtube import FakeVideoUploader

KEY = "0f41035d6144c07b16f28327" + "a" * 40
MARKER = upload_marker(KEY)
assert MARKER == "avpu0f41035d6144c07b16f28327"


@pytest.fixture
def flag_on(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(UPLOAD_DESCRIPTION_WITHOUT_MARKER_ENV, "1")


def _build(**overrides: object):
    args: dict[str, object] = {
        "upload_key": KEY,
        "title": "t",
        "hook": "h",
        "narration": "n",
        "language": "ja",
    }
    args.update(overrides)
    return build_youtube_metadata(**args)  # type: ignore[arg-type]


def test_flag_is_off_by_default(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv(UPLOAD_DESCRIPTION_WITHOUT_MARKER_ENV, raising=False)
    assert description_without_marker_enabled() is False
    assert description_without_marker_enabled({UPLOAD_DESCRIPTION_WITHOUT_MARKER_ENV: "0"}) is False
    assert description_without_marker_enabled({UPLOAD_DESCRIPTION_WITHOUT_MARKER_ENV: "true"})


def test_flag_off_is_byte_identical_to_the_legacy_shape(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv(UPLOAD_DESCRIPTION_WITHOUT_MARKER_ENV, raising=False)
    meta = _build()
    assert meta.description == f"h\n\nn\n\n{MARKER}"
    assert meta.tags == (MARKER,)


_UUID = str(uuid.UUID("12345678-1234-5678-1234-567812345678"))


@pytest.mark.parametrize(
    "leak",
    [
        "avpu0f41035d6144c07b16f28327",
        f"see {_UUID} here",
        "minio://artifacts/media/ep1/final_video/" + "a" * 64 + ".mp4",
        "s3://artifacts/artifacts/ep1/script/" + "b" * 64 + ".json",
        "media/ep_1/final_video/" + "c" * 64 + ".mp4",
        "artifacts/ep_1/script/" + "d" * 64 + ".json",
    ],
)
def test_description_never_contains_internal_ids(flag_on: None, leak: str) -> None:
    """T17: 本文・必須部分のどちらに内部 ID があっても組み立ての時点で止まる。"""
    assert find_internal_ids(leak)
    with pytest.raises(InternalIdInDescriptionError):
        _build(narration=f"text {leak} text")
    with pytest.raises(InternalIdInDescriptionError):
        _build(required_parts=(f"Music: {leak}",))


def test_lint_leaves_ordinary_text_alone(flag_on: None) -> None:
    assert find_internal_ids("Kyoto, 1185. avpu is not an id. 0f41035d6144c07b16f28327") == []
    meta = _build(narration="普通の本文 https://example.org/source-1")
    assert not find_internal_ids(meta.description)


def test_flag_off_does_not_lint(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv(UPLOAD_DESCRIPTION_WITHOUT_MARKER_ENV, raising=False)
    assert _UUID in _build(narration=_UUID).description


def test_insert_body_description_has_no_marker(flag_on: None) -> None:
    body = insert_body(_build())
    assert MARKER not in body["snippet"]["description"]
    assert not find_internal_ids(body["snippet"]["description"])


def test_insert_body_lints_the_final_body(flag_on: None) -> None:
    """builder を通らずに作った metadata（旧 snapshot など）も送信直前で止める。"""
    meta = YouTubeVideoMetadata(
        title="t",
        description=f"body\n\n{MARKER}",
        tags=(MARKER,),
        category_id="22",
        privacy_status="private",
        self_declared_made_for_kids=False,
        contains_synthetic_media=True,
        notify_subscribers=False,
    )
    with pytest.raises(InternalIdInDescriptionError):
        insert_body(meta)


def test_insert_body_is_unchanged_with_flag_off(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv(UPLOAD_DESCRIPTION_WITHOUT_MARKER_ENV, raising=False)
    body = insert_body(_build())
    assert body["snippet"]["description"].endswith(MARKER)
    assert body["snippet"]["tags"] == [MARKER]


@pytest.mark.parametrize("on", [True, False])
def test_tags_keep_the_reconciliation_marker(monkeypatch: pytest.MonkeyPatch, on: bool) -> None:
    """T18（fake レベル）: description の変更は照合キーを変えない。description とは別に固定する。"""
    if on:
        monkeypatch.setenv(UPLOAD_DESCRIPTION_WITHOUT_MARKER_ENV, "1")
    else:
        monkeypatch.delenv(UPLOAD_DESCRIPTION_WITHOUT_MARKER_ENV, raising=False)
    meta = _build()
    assert meta.tags == (MARKER,)
    assert insert_body(meta)["snippet"]["tags"] == [MARKER]


async def test_lookup_matches_tags_only_and_legacy_description_only_videos() -> None:
    """T18/T19: find_video_by_marker は変えない。tags のみ・旧 description のみの両方に一致する。"""
    fake = FakeVideoUploader()
    other = fake.add_existing_video(["avpu" + "f" * 24], description="unrelated")
    tags_only = fake.add_existing_video([MARKER], description="viewer text only")
    assert await fake.find_video_by_marker(MARKER) == tags_only

    fake2 = FakeVideoUploader()
    legacy = fake2.add_existing_video([], description=f"old body\n\n{MARKER}")
    assert await fake2.find_video_by_marker(MARKER) == legacy
    assert await fake2.find_video_by_marker("avpu" + "e" * 24) is None
    assert other != tags_only


def test_required_parts_over_the_limit_raise_instead_of_truncating(flag_on: None) -> None:
    """T29: クレジットだけで 5000 bytes を超えるなら切らずに例外（needs_input の入口）。"""
    with pytest.raises(DescriptionRequiredPartsTooLongError):
        _build(required_parts=("あ" * 1700,))  # 5100 bytes


def test_required_parts_survive_a_long_body(flag_on: None) -> None:
    credit = "Music: Example Artist - Example Song (CC BY 4.0) https://example.org/license"
    meta = _build(narration="あ" * 5000, required_parts=(credit,))
    assert meta.description.endswith(credit)
    assert len(meta.description.encode("utf-8")) <= YOUTUBE_DESCRIPTION_MAX_BYTES
    assert MARKER not in meta.description


def test_required_parts_exactly_filling_the_budget_are_kept(flag_on: None) -> None:
    credit = "x" * YOUTUBE_DESCRIPTION_MAX_BYTES
    meta = _build(required_parts=(credit,))
    assert meta.description == credit


_SHA = "a1" * 32
_ZW = "​"


@pytest.mark.parametrize(
    "leak",
    [
        f"動画ID{_UUID}",
        f"id_{_UUID}",
        f"ID:{_UUID.upper()}",
        "12345678123456781234567812345678",
        "動画12345678123456781234567812345678です",
        _SHA,
        f"保存先media/ep_1/final_video/{_SHA}.mp4",
        f"https://example.org/artifacts/ep_1/script/{_SHA}.json",
        "AVPU0F41035D6144C07B16F28327",
        "ａｖｐｕ０ｆ４１０３５ｄ６１４４ｃ０７ｂ１６ｆ２８３２７",
        f"avpu0f41035d6144c07b16{_ZW}f28327",
        f"a{_ZW}vpu0f41035d6144c07b16f28327",
        "S3://bucket/key",
    ],
)
def test_lint_catches_obfuscated_and_adjacent_internal_ids(leak: str) -> None:
    """レビュー M1: \\b の取りこぼし・大文字・全角・ゼロ幅文字・CJK 隣接・sha・URL 内の key。"""
    assert find_internal_ids(leak), leak


@pytest.mark.parametrize(
    "text",
    [
        "How did one decision change Japanese history? #JapaneseHistory #HistoryShorts",
        "京都は1185年に動いた。詳しくは https://example.org/source-1 と https://ja.wikipedia.org/wiki/源平合戦",
        "Music: Example Artist - Example Song (CC BY 4.0) https://example.org/license",
        "commit 0f41035d6144c07b16f28327 is 24 hex; abc123 is short",
        "media/ep/x is a plain path, artifacts are great",
        "e" * 31,
    ],
)
def test_lint_has_no_false_positive_on_normal_text(text: str) -> None:
    assert find_internal_ids(text) == []


def test_empty_body_with_required_parts_has_no_leading_separator(flag_on: None) -> None:
    meta = _build(hook="", narration="", required_parts=("Music: Example",))
    assert meta.description == "Music: Example"
