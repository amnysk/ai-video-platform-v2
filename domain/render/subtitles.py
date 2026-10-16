"""字幕 cue の割り付け（ADR-0019 §4）。純粋関数のみ。

- 字幕の真実は台本ナレーション。cue は ``ScriptScene.narration`` への文字オフセットだけを持つ
- 言語に依存しない分割: 文末記号（``。！？`` / ``.!?``）で文に分け、
  ``max_chars_per_line`` で行に折り返し（空白があれば語の境界、無ければ文字の境界）、
  ``max_lines`` 行ずつ1枚の cue にまとめる
- 時刻は音声の窓の中で文字数に比例して割り付ける
- 表示文は描画時に ``cue_display_text`` で台本から具体化する（計画には保存しない）
"""

from __future__ import annotations

from collections.abc import Sequence

from contracts.artifacts import ScriptArtifact
from contracts.render import RenderSubtitleSettings, RenderVoicePlacement, SubtitleCue
from domain.errors import VoiceTimelineOverflowError

#: 常に文末とみなす記号（全角）。
_ALWAYS_TERMINATORS = frozenset("。！？‼⁇⁈⁉")
#: 後ろが空白・終端・閉じ括弧のときだけ文末とみなす記号（``3.14`` を割らない）。
_ASCII_TERMINATORS = frozenset(".!?")
#: 文末記号の直後に続けて同じ文へ含める閉じ記号。
_CLOSERS = frozenset("」』）)]】\"'”’")
_TERMINATORS = _ALWAYS_TERMINATORS | _ASCII_TERMINATORS

Range = tuple[int, int]


def _strip(text: str, start: int, end: int) -> Range:
    while start < end and text[start].isspace():
        start += 1
    while end > start and text[end - 1].isspace():
        end -= 1
    return start, end


def sentence_ranges(text: str) -> list[Range]:
    """文の範囲 ``[start, end)``。前後の空白は含めない。空の文は返さない。"""
    ranges: list[Range] = []
    n = len(text)
    start = 0
    i = 0
    while i < n:
        ch = text[i]
        is_end = ch in _ALWAYS_TERMINATORS or (
            ch in _ASCII_TERMINATORS
            and (i + 1 == n or text[i + 1].isspace() or text[i + 1] in _CLOSERS | _TERMINATORS)
        )
        if is_end:
            j = i + 1
            while j < n and (text[j] in _TERMINATORS or text[j] in _CLOSERS):
                j += 1
            ranges.append((start, j))
            start = i = j
            continue
        i += 1
    if start < n:
        ranges.append((start, n))
    return [r for r in (_strip(text, s, e) for s, e in ranges) if r[0] < r[1]]


def wrap_lines(text: str, start: int, end: int, max_chars_per_line: int) -> list[Range]:
    """``[start, end)`` を1行 ``max_chars_per_line`` 文字以内に貪欲に折り返す。

    行の途中で語が割れる位置なら、その行の中の最後の空白で折る（空白が無ければ文字で折る）。
    行の前後の空白は含めない。
    """
    lines: list[Range] = []
    pos = start
    while pos < end:
        while pos < end and text[pos].isspace():
            pos += 1
        if pos >= end:
            break
        limit = pos + max_chars_per_line
        if limit >= end:
            stop = end
        else:
            stop = limit
            if not text[limit].isspace() and not text[limit - 1].isspace():
                for k in range(limit - 1, pos, -1):
                    if text[k].isspace():
                        stop = k
                        break
        line = _strip(text, pos, stop)
        if line[0] < line[1]:
            lines.append(line)
        pos = stop
    return lines


def cue_ranges(text: str, settings: RenderSubtitleSettings) -> list[Range]:
    """ナレーション1件を cue の文字範囲に分ける。"""
    ranges: list[Range] = []
    for s_start, s_end in sentence_ranges(text):
        lines = wrap_lines(text, s_start, s_end, settings.max_chars_per_line)
        for i in range(0, len(lines), settings.max_lines):
            group = lines[i : i + settings.max_lines]
            ranges.append((group[0][0], group[-1][1]))
    return ranges


def _cue_times(start_ms: int, duration_ms: int, weights: Sequence[int]) -> list[Range]:
    """窓 ``[start_ms, start_ms + duration_ms)`` を重みに比例して連続に割る。"""
    if duration_ms < len(weights):
        raise VoiceTimelineOverflowError(
            f"voice of {duration_ms} ms is too short for {len(weights)} subtitle cues"
        )
    total = sum(weights)
    bounds = [start_ms]
    cumulative = 0
    for index, weight in enumerate(weights):
        cumulative += weight
        remaining = len(weights) - index - 1
        bound = start_ms + duration_ms * cumulative // total
        # 各 cue に最低 1ms を残す（丸めで長さ0の cue を作らない）
        bound = max(bound, bounds[-1] + 1)
        bound = min(bound, start_ms + duration_ms - remaining)
        bounds.append(bound)
    return list(zip(bounds[:-1], bounds[1:], strict=True))


def build_subtitle_cues(
    script: ScriptArtifact,
    voices: Sequence[RenderVoicePlacement],
    settings: RenderSubtitleSettings,
) -> tuple[SubtitleCue, ...]:
    """音声の順に cue を組む。字幕が無効なら空。"""
    if not settings.enabled:
        return ()
    narration = {scene.id: scene.narration for scene in script.scenes}
    cues: list[SubtitleCue] = []
    for voice in voices:
        text = narration[voice.script_scene_id]
        ranges = cue_ranges(text, settings)
        if not ranges:
            continue
        times = _cue_times(voice.start_ms, voice.duration_ms, [e - s for s, e in ranges])
        for (char_start, char_end), (start_ms, end_ms) in zip(ranges, times, strict=True):
            cues.append(
                SubtitleCue(
                    cue_index=len(cues),
                    script_scene_id=voice.script_scene_id,
                    char_start=char_start,
                    char_end=char_end,
                    start_ms=start_ms,
                    end_ms=end_ms,
                )
            )
    return tuple(cues)


def cue_display_text(
    script: ScriptArtifact, cue: SubtitleCue, settings: RenderSubtitleSettings
) -> str:
    """cue の表示文（行は ``\\n`` 区切り）。台本から毎回具体化する。"""
    narration = {scene.id: scene.narration for scene in script.scenes}[cue.script_scene_id]
    if cue.char_end > len(narration):
        raise ValueError(f"cue {cue.cue_index} exceeds the narration of {cue.script_scene_id}")
    lines = wrap_lines(narration, cue.char_start, cue.char_end, settings.max_chars_per_line)
    return "\n".join(narration[s:e] for s, e in lines)


def subtitle_display_texts(
    script: ScriptArtifact, cues: Sequence[SubtitleCue], settings: RenderSubtitleSettings
) -> list[str]:
    return [cue_display_text(script, cue, settings) for cue in cues]


__all__ = [
    "build_subtitle_cues",
    "cue_display_text",
    "cue_ranges",
    "sentence_ranges",
    "subtitle_display_texts",
    "wrap_lines",
]
