"""字幕ファイル（ASS）の組み立て（純粋関数）とフォントの family 名の読み取り。

- 画面座標は profile の解像度そのもの（PlayResX/Y = 幅/高さ）。配置は profile.subtitles が決める
- 本文は呼び出し側が台本ナレーションから具体化した表示文字列。
  ここは装飾の注入を防ぐ escape だけを行う
- 時刻は ASS の centisecond。ミリ秒を四捨五入し、終了が開始以下にならないよう 1cs は必ず残す
"""

from __future__ import annotations

import struct
from collections.abc import Sequence
from pathlib import Path

from contracts.render import RenderProfile, SubtitleCue

_ALIGNMENT = {"bottom_center": 2, "middle_center": 5, "top_center": 8}


class FontReadError(Exception):
    """フォントファイルから family 名を読めない。"""


def ass_timestamp(ms: int) -> str:
    cs = (ms + 5) // 10
    hours, rest = divmod(cs, 360_000)
    minutes, rest = divmod(rest, 6_000)
    seconds, centis = divmod(rest, 100)
    return f"{hours}:{minutes:02d}:{seconds:02d}.{centis:02d}"


def escape_ass_text(text: str) -> str:
    """表示文字列を ASS の Dialogue 本文へ安全に埋める。

    ``{...}`` は装飾命令、``\\N`` 等は制御になるので、
    利用者の文字列が命令として解釈されないようにする。
    改行は ``\\N``（明示改行）へ変換する。
    """
    out = text.replace("\r\n", "\n").replace("\r", "\n")
    # バックスラッシュの直後に単語結合子を挟み、\N / \h などの制御列にならないようにする
    out = out.replace("\\", "\\⁠")
    out = out.replace("{", "\\{").replace("}", "\\}")
    return out.replace("\n", "\\N")


def build_ass(
    profile: RenderProfile,
    cues: Sequence[SubtitleCue],
    texts: Sequence[str],
    *,
    font_family: str,
) -> str:
    if len(cues) != len(texts):
        raise ValueError("subtitle texts must align with subtitle cues")
    if any(ch in font_family for ch in ",\n\r"):
        raise ValueError("font family name must not contain commas or newlines")
    subs = profile.subtitles
    outline = max(1, round(subs.font_size_px / 16))
    lines = [
        "[Script Info]",
        "ScriptType: v4.00+",
        f"PlayResX: {profile.width}",
        f"PlayResY: {profile.height}",
        "WrapStyle: 0",
        "ScaledBorderAndShadow: yes",
        "YCbCr Matrix: None",
        "",
        "[V4+ Styles]",
        "Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, "
        "BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, "
        "BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding",
        f"Style: Default,{font_family},{subs.font_size_px},&H00FFFFFF,&H000000FF,&H00000000,"
        f"&H80000000,0,0,0,0,100,100,0,0,1,{outline},0,{_ALIGNMENT[subs.alignment]},"
        f"{subs.margin_side_px},{subs.margin_side_px},{subs.margin_bottom_px},1",
        "",
        "[Events]",
        "Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text",
    ]
    for cue, text in zip(cues, texts, strict=True):
        start = ass_timestamp(cue.start_ms)
        end_cs = max((cue.end_ms + 5) // 10, (cue.start_ms + 5) // 10 + 1)
        end = ass_timestamp(end_cs * 10)
        lines.append(f"Dialogue: 0,{start},{end},Default,,0,0,0,,{escape_ass_text(text)}")
    return "\n".join(lines) + "\n"


def read_font_family(path: Path) -> str:
    """OpenType / TrueType（TTC は先頭の face）の name テーブルから family 名を読む。

    typographic family（nameID 16）を優先し、無ければ nameID 1。Windows（UTF-16BE）の
    英語名を優先する。libass にフォントを名前で選ばせるために使う。
    """
    try:
        with path.open("rb") as handle:
            header = _read(handle, 0, 12)
            offset = 0
            if header[:4] == b"ttcf":
                offset = struct.unpack(">I", _read(handle, 12, 4))[0]
                header = _read(handle, offset, 12)
            num_tables = struct.unpack(">H", header[4:6])[0]
            directory = _read(handle, offset + 12, 16 * num_tables)
            for i in range(num_tables):
                tag, _checksum, table_offset, length = struct.unpack(
                    ">4sIII", directory[16 * i : 16 * (i + 1)]
                )
                if tag == b"name":
                    return _family_from_name_table(_read(handle, table_offset, length))
    except (OSError, struct.error) as exc:
        raise FontReadError(f"cannot read font {path}: {exc}") from exc
    raise FontReadError(f"font {path} has no name table")


def _read(handle, offset: int, size: int) -> bytes:  # type: ignore[no-untyped-def]
    handle.seek(offset)
    data = handle.read(size)
    if len(data) != size:
        raise FontReadError("truncated font file")
    return data


def _family_from_name_table(table: bytes) -> str:
    _format, count, string_offset = struct.unpack(">HHH", table[:6])
    candidates: list[tuple[int, str]] = []
    for i in range(count):
        platform, encoding, language, name_id, length, offset = struct.unpack(
            ">HHHHHH", table[6 + 12 * i : 18 + 12 * i]
        )
        if name_id not in (1, 16):
            continue
        raw = table[string_offset + offset : string_offset + offset + length]
        if platform == 3 and encoding in (1, 10):
            name = raw.decode("utf-16-be", errors="strict")
            rank = 0 if language == 0x409 else 2
        elif platform == 1 and encoding == 0:
            name = raw.decode("latin-1")
            rank = 4
        else:
            continue
        # nameID 16 を同じ条件の nameID 1 より先に
        candidates.append((rank + (0 if name_id == 16 else 1), name))
    names = [name for _rank, name in sorted(candidates, key=lambda c: c[0]) if name.strip()]
    if not names:
        raise FontReadError("font has no family name")
    return names[0]


__all__ = ["FontReadError", "ass_timestamp", "build_ass", "escape_ass_text", "read_font_family"]
