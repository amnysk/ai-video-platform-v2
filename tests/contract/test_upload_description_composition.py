"""構成された description（導入文・補足・参照・クレジット。ADR-0075、INV-111 / INV-112）。

理由は docs/testing/upload-description-composition-rationale.md。
"""

from __future__ import annotations

import pytest

from contracts.music import BgmTrack  # noqa: F401  (クレジット文の出所。型は str)
from contracts.upload import (
    DESCRIPTION_INTRO_MIN_BYTES,
    UPLOAD_DESCRIPTION_COMPOSITION_ENV,
    UPLOAD_DESCRIPTION_WITHOUT_MARKER_ENV,
    YOUTUBE_DESCRIPTION_MAX_BYTES,
    DescriptionBlocked,
    DescriptionBlockedError,
    DescriptionBlockReason,
    DescriptionComposed,
    DescriptionComposition,
    DescriptionReference,
    build_youtube_metadata,
    compose_description,
    find_internal_ids,
    upload_marker,
)
from infrastructure.youtube.metadata import insert_body

KEY = "0f41035d6144c07b16f28327" + "a" * 40
MARKER = upload_marker(KEY)
INTRO = "In 1600 the armies of east and west met at Sekigahara, and one day decided a century."
CREDIT = "Music: Example Track by Someone (CC BY 4.0) https://example.com/track"
REF = DescriptionReference(title="Sekigahara, a history", url="https://example.com/book")


def _comp(**kw: object) -> DescriptionComposition:
    args: dict[str, object] = {"intro": INTRO}
    args.update(kw)
    return DescriptionComposition(**args)  # type: ignore[arg-type]


def _ok(result) -> DescriptionComposed:
    assert isinstance(result, DescriptionComposed), result
    return result


def test_order_is_intro_supplement_references_credits_marker() -> None:
    comp = _comp(supplements=("Filmed with AI tools.",), references=(REF,), credits=(CREDIT,))
    text = _ok(compose_description(comp, marker=MARKER)).description
    parts = text.split("\n\n")
    assert parts == [
        INTRO,
        "Filmed with AI tools.",
        "References:\n- Sekigahara, a history - https://example.com/book",
        CREDIT,
        MARKER,
    ]


def test_without_marker_there_is_no_marker_line() -> None:
    text = _ok(compose_description(_comp(credits=(CREDIT,)))).description
    assert MARKER not in text and text.endswith(CREDIT)


def test_composition_is_deterministic() -> None:
    comp = _comp(references=(REF,), credits=(CREDIT,))
    assert compose_description(comp, marker=MARKER) == compose_description(comp, marker=MARKER)


def test_credit_lines_are_kept_verbatim() -> None:
    credit = "Music: “Ünï” by A & B — https://example.com/?a=1&b=2"
    text = _ok(compose_description(_comp(credits=(credit,)))).description
    assert credit in text


def test_credit_with_angle_brackets_blocks_instead_of_being_rewritten() -> None:
    result = compose_description(_comp(credits=("Music: <b>X</b>",)))
    assert isinstance(result, DescriptionBlocked)
    assert result.reason is DescriptionBlockReason.CREDIT_NOT_REPRESENTABLE


def test_required_references_missing_blocks() -> None:
    result = compose_description(_comp(references_required=True))
    assert isinstance(result, DescriptionBlocked)
    assert result.reason is DescriptionBlockReason.REFERENCES_MISSING


def test_required_parts_over_the_limit_block_and_never_truncate() -> None:
    big = tuple(f"Music: {'x' * 400} #{i}" for i in range(14))  # > 5000 bytes
    result = compose_description(_comp(credits=big), marker=MARKER)
    assert isinstance(result, DescriptionBlocked)
    assert result.reason is DescriptionBlockReason.REQUIRED_PARTS_TOO_LONG


def test_not_enough_room_for_the_intro_blocks() -> None:
    fill = "Music: " + "x" * (YOUTUBE_DESCRIPTION_MAX_BYTES - 7 - DESCRIPTION_INTRO_MIN_BYTES + 20)
    result = compose_description(_comp(credits=(fill,)))
    assert isinstance(result, DescriptionBlocked)
    assert result.reason is DescriptionBlockReason.INTRO_NO_ROOM


def test_long_optional_parts_are_trimmed_but_required_parts_survive() -> None:
    comp = _comp(
        intro="Sentence one. " * 400,
        supplements=("Extra info.",),
        references=(REF,),
        credits=(CREDIT,),
    )
    result = _ok(compose_description(comp, marker=MARKER))
    assert result.optional_trimmed
    assert len(result.description.encode()) <= YOUTUBE_DESCRIPTION_MAX_BYTES
    assert "Extra info." not in result.description
    for required in ("References:", REF.title, CREDIT, MARKER):
        assert required in result.description
    assert result.description.endswith(MARKER)


def test_whole_supplements_are_dropped_from_the_end_not_cut() -> None:
    comp = _comp(
        intro="Z" * 3000,  # hex 文字は内部 ID の lint に掛かるので使わない
        supplements=("Y" * 1500, "X" * 1500, "W" * 10),
        credits=(CREDIT,),
    )
    text = _ok(compose_description(comp)).description
    assert "Y" * 1500 in text and "X" * 1500 not in text and "W" * 10 not in text


@pytest.mark.parametrize(
    "bad",
    [
        "see minio://artifacts/x",
        "id 123e4567-e89b-12d3-a456-426614174000",
        "hash " + "a" * 64,
        "tag avpu0f41035d6144c07b16f28327",
    ],
)
def test_internal_ids_in_any_part_block(bad: str) -> None:
    for kw in ({"intro": bad}, {"supplements": (bad,)}, {"credits": (bad,)}):
        result = compose_description(_comp(**kw), marker=MARKER)
        assert isinstance(result, DescriptionBlocked), kw
        assert result.reason is DescriptionBlockReason.INTERNAL_ID
        assert bad not in result.detail


def test_the_marker_line_itself_is_not_an_internal_id_violation() -> None:
    assert find_internal_ids(MARKER)  # lint は marker を検出する
    assert isinstance(compose_description(_comp(), marker=MARKER), DescriptionComposed)


def test_reference_and_composition_shapes_are_validated() -> None:
    with pytest.raises(ValueError):
        DescriptionReference(title="x", url="ftp://example.com")
    with pytest.raises(ValueError):
        DescriptionReference(title="two\nlines")
    with pytest.raises(ValueError):
        DescriptionComposition(intro="  ")
    with pytest.raises(ValueError):
        DescriptionComposition(intro="ok", credits=(" ",))


# ------------------------------------------------------------- build_youtube_metadata との結線


def _build(**kw: object):
    args: dict[str, object] = {
        "upload_key": KEY,
        "title": "t",
        "hook": "h",
        "narration": "FULL NARRATION " * 50,
        "language": "en",
    }
    args.update(kw)
    return build_youtube_metadata(**args)  # type: ignore[arg-type]


def test_flag_off_ignores_the_composition(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv(UPLOAD_DESCRIPTION_COMPOSITION_ENV, raising=False)
    assert _build(composition=_comp(credits=(CREDIT,))) == _build()


def test_flag_on_uses_the_composition_and_not_the_narration(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv(UPLOAD_DESCRIPTION_COMPOSITION_ENV, "1")
    meta = _build(composition=_comp(references=(REF,), credits=(CREDIT,)))
    assert "FULL NARRATION" not in meta.description
    assert meta.description.startswith(INTRO)
    assert meta.description.endswith(MARKER)  # marker flag は OFF（既定）なので最終行に残る
    assert meta.tags == (MARKER,) and meta.privacy_status == "private"


def test_flag_on_with_marker_removed_keeps_the_lint_and_the_tag(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv(UPLOAD_DESCRIPTION_COMPOSITION_ENV, "1")
    monkeypatch.setenv(UPLOAD_DESCRIPTION_WITHOUT_MARKER_ENV, "1")
    meta = _build(composition=_comp(credits=(CREDIT,)))
    assert MARKER not in meta.description and meta.tags == (MARKER,)
    assert not find_internal_ids(meta.description)
    insert_body(meta)  # 送信直前の lint も通る
    with pytest.raises(DescriptionBlockedError) as info:
        _build(composition=_comp(intro="leak avpu0f41035d6144c07b16f28327"))
    assert info.value.reason is DescriptionBlockReason.INTERNAL_ID


def test_flag_on_blocked_composition_raises_instead_of_truncating(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv(UPLOAD_DESCRIPTION_COMPOSITION_ENV, "1")
    big = tuple(f"Music: {'x' * 400} #{i}" for i in range(14))
    with pytest.raises(DescriptionBlockedError) as info:
        _build(composition=_comp(credits=big))
    assert info.value.reason is DescriptionBlockReason.REQUIRED_PARTS_TOO_LONG


def test_music_credit_text_from_the_music_contract_is_a_plain_string() -> None:
    """クレジット行の入力型は ``contracts/music.py`` の帰属文（str）と同じ。"""
    assert BgmTrack.model_fields["exact_attribution_text"].annotation == (str | None)
