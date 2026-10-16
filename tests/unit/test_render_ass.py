"""ASS の組み立てとフォント family 名の読み取り。"""

from __future__ import annotations

import struct
from pathlib import Path

import pytest

from infrastructure.render.ass import (
    FontReadError,
    ass_timestamp,
    build_ass,
    escape_ass_text,
    read_font_family,
)
from tests.support.render_plans import make_plan

NOTO = Path("/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc")

SHORTS_SNAPSHOT = """\
[Script Info]
ScriptType: v4.00+
PlayResX: 1080
PlayResY: 1920
WrapStyle: 0
ScaledBorderAndShadow: yes
YCbCr Matrix: None

[V4+ Styles]
Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding
Style: Default,Noto Sans CJK JP,56,&H00FFFFFF,&H000000FF,&H00000000,&H80000000,0,0,0,0,100,100,0,0,1,4,0,2,80,80,420,1

[Events]
Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text
Dialogue: 0,0:00:00.00,0:00:00.90,Default,,0,0,0,,こんにちは
Dialogue: 0,0:00:01.10,0:00:01.80,Default,,0,0,0,,二行目\\Nです
"""  # noqa: E501


def _plan(profile: str):
    return make_plan(
        profile=profile,
        scenes=((1000, 1000), (1000, 1000)),
        voices=((0, 900), (1100, 700)),
        cues=((0, 0, 900), (1, 1100, 1800)),
    )


def test_shorts_snapshot() -> None:
    plan = _plan("shorts_vertical")
    text = build_ass(
        plan.profile,
        plan.subtitle_cues,
        ["こんにちは", "二行目\nです"],
        font_family="Noto Sans CJK JP",
    )
    assert text == SHORTS_SNAPSHOT


def test_long_form_layout_comes_from_the_profile() -> None:
    plan = _plan("long_form_horizontal")
    text = build_ass(plan.profile, plan.subtitle_cues, ["a", "b"], font_family="X")
    assert "PlayResX: 1920\nPlayResY: 1080\n" in text
    assert "Style: Default,X,48," in text and ",1,3,0,2,160,160,86,1\n" in text


def test_alignment_and_text_count() -> None:
    plan = _plan("shorts_vertical")
    subs = plan.profile.subtitles.model_copy(update={"alignment": "top_center"})
    profile = plan.profile.model_copy(update={"subtitles": subs})
    assert ",0,8,80,80,420,1\n" in build_ass(
        profile, plan.subtitle_cues, ["a", "b"], font_family="X"
    )
    with pytest.raises(ValueError):
        build_ass(profile, plan.subtitle_cues, ["a"], font_family="X")
    with pytest.raises(ValueError):
        build_ass(profile, plan.subtitle_cues, ["a", "b"], font_family="A,B")


def test_text_cannot_inject_override_tags() -> None:
    escaped = escape_ass_text("{\\b1}bold\\Nx\r\ny")
    assert "{" not in escaped.replace("\\{", "")
    assert "\\N" in escaped  # 本物の改行だけが \N になる
    assert escaped.count("\\N") == 1
    assert escaped.endswith("\\Ny")


@pytest.mark.parametrize(
    ("ms", "text"),
    [(0, "0:00:00.00"), (4, "0:00:00.00"), (5, "0:00:00.01"), (61_234, "0:01:01.23"),
     (3_600_000, "1:00:00.00")],
)  # fmt: skip
def test_timestamp(ms: int, text: str) -> None:
    assert ass_timestamp(ms) == text


def test_cue_shorter_than_a_centisecond_still_has_positive_length() -> None:
    plan = make_plan(cues=((0, 100, 103),))
    text = build_ass(plan.profile, plan.subtitle_cues, ["x"], font_family="X")
    assert "Dialogue: 0,0:00:00.10,0:00:00.11," in text


def _minimal_font(family: str) -> bytes:
    encoded = family.encode("utf-16-be")
    name_table = (
        struct.pack(">HHH", 0, 1, 6 + 12)
        + struct.pack(">HHHHHH", 3, 1, 0x409, 1, len(encoded), 0)
        + encoded
    )
    offset = 12 + 16
    header = struct.pack(">IHHHH", 0x00010000, 1, 0, 0, 0)
    record = struct.pack(">4sIII", b"name", 0, offset, len(name_table))
    return header + record + name_table


def test_reads_family_from_a_minimal_sfnt(tmp_path: Path) -> None:
    font = tmp_path / "f.ttf"
    font.write_bytes(_minimal_font("Test Family"))
    assert read_font_family(font) == "Test Family"


def test_unreadable_font(tmp_path: Path) -> None:
    bad = tmp_path / "bad.ttf"
    bad.write_bytes(b"\x00\x01")
    with pytest.raises(FontReadError):
        read_font_family(bad)
    with pytest.raises(FontReadError):
        read_font_family(tmp_path / "missing.ttf")


@pytest.mark.skipif(not NOTO.is_file(), reason="Noto CJK not installed")
def test_reads_noto_cjk_collection_family() -> None:
    assert read_font_family(NOTO) == "Noto Sans CJK JP"
