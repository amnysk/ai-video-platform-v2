"""flag OFF の description は、構成の導入前と byte 単位で同じ（INV-111、ADR-0075）。

期待値は導入前のコード（commit 6d14c30 の ``build_youtube_metadata``）が出した値の固定。
このファイルの値を変えるなら、現行の description を変える判断（ADR）が先に要る。
"""

from __future__ import annotations

import hashlib

import pytest

from contracts.upload import (
    UPLOAD_DESCRIPTION_COMPOSITION_ENV,
    build_youtube_metadata,
    description_composition_enabled,
)

KEY = "0f41035d6144c07b16f28327" + "a" * 40
MARKER = "avpu0f41035d6144c07b16f28327"


def _build(marker: bool, **args: object) -> str:
    return build_youtube_metadata(
        upload_key=KEY,
        language="ja",
        marker_in_description=marker,
        **args,  # type: ignore[arg-type]
    ).description


def _digest(text: str) -> tuple[int, str]:
    return len(text.encode()), hashlib.sha256(text.encode()).hexdigest()


def test_the_composition_flag_is_off_by_default_and_by_any_non_truthy_value() -> None:
    assert description_composition_enabled({}) is False
    for value in ("", "0", "false", "no", "off", " "):
        assert description_composition_enabled({UPLOAD_DESCRIPTION_COMPOSITION_ENV: value}) is False
    for value in ("1", "true", "TRUE", " 1 "):
        assert description_composition_enabled({UPLOAD_DESCRIPTION_COMPOSITION_ENV: value})


@pytest.fixture(autouse=True)
def _flag_unset(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv(UPLOAD_DESCRIPTION_COMPOSITION_ENV, raising=False)


def test_short_with_and_without_marker() -> None:
    kw = {"title": "Title", "hook": "Hook line", "narration": "Narration body."}
    assert _build(True, **kw) == f"Hook line\n\nNarration body.\n\n{MARKER}"
    assert _build(False, **kw) == "Hook line\n\nNarration body."


def test_japanese_body_is_sanitized_the_same_way() -> None:
    kw = {"title": "タイトル", "hook": "フック", "narration": "本文です。<tag>"}
    assert _build(True, **kw) == f"フック\n\n本文です。＜tag＞\n\n{MARKER}"
    assert _build(False, **kw) == "フック\n\n本文です。＜tag＞"


def test_long_body_is_truncated_to_the_same_bytes() -> None:
    kw = {"title": "T", "hook": "H", "narration": "あ" * 3000}
    assert _digest(_build(True, **kw)) == (
        4998,
        "91f115a9d487868557e0c372c9353e86d39d1f757dd5795db7ff754b69fc9c78",
    )
    assert _digest(_build(False, **kw)) == (
        4998,
        "d5f24fec9afaad9f0b44449dd6c2d48bc962a03b43352b99b75110f043e68886",
    )


def test_required_parts_layout_is_unchanged() -> None:
    kw = {"title": "T", "hook": "H", "narration": "n" * 6000, "required_parts": ("Music: Example",)}
    assert _digest(_build(True, **kw)) == (
        5000,
        "ac42d84356ccbbbc9763df59560d76f3a153393e0c2ae5a5bb0f3b352948d981",
    )
    assert _digest(_build(False, **kw)) == (
        5000,
        "a707b14c06b981f56ec71a6e2c6fd9560c43ddbee5f136312dd140cc54b30a65",
    )


def test_empty_body_shapes_are_unchanged() -> None:
    kw = {"title": "T", "hook": "", "narration": ""}
    assert _build(True, **kw) == f"\n\n{MARKER}"
    assert _build(False, **kw) == ""


def test_flag_on_without_a_composition_input_is_still_the_legacy_description(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """flag を ON にしても、構成の入力（composition）を渡さない呼び出しは従来どおり。"""
    legacy = _build(True, title="Title", hook="Hook line", narration="Narration body.")
    monkeypatch.setenv(UPLOAD_DESCRIPTION_COMPOSITION_ENV, "1")
    assert _build(True, title="Title", hook="Hook line", narration="Narration body.") == legacy
