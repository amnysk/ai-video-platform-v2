"""字幕 cue の割り付け（ADR-0019 §4）。"""

from __future__ import annotations

import uuid

import pytest

from contracts.artifacts import ScriptArtifact, build_script_artifact, parse_script_artifact
from contracts.render import (
    ArtifactDigestRef,
    RenderSubtitleSettings,
    RenderVoicePlacement,
    get_render_profile,
)
from domain.errors import VoiceTimelineOverflowError
from domain.render.subtitles import (
    build_subtitle_cues,
    cue_display_text,
    cue_ranges,
    sentence_ranges,
    subtitle_display_texts,
    wrap_lines,
)

REF = ArtifactDigestRef(artifact_id=str(uuid.uuid4()), sha256="d" * 64)


def _settings(**over: object) -> RenderSubtitleSettings:
    base = get_render_profile("shorts_vertical").subtitles
    return base.model_copy(update=over)


def _script(language: str, narrations: list[str]) -> ScriptArtifact:
    return parse_script_artifact(
        build_script_artifact(
            episode_id="ep",
            language=language,
            title="t",
            hook="h",
            scenes=[
                {"id": f"s{i}", "narration": n, "visual": "v", "duration_ms": 8000}
                for i, n in enumerate(narrations, start=1)
            ],
            metadata={"topic": "t", "generator": "g", "generator_model": "m"},
        )
    )


def _voice(sid: str, start: int, duration: int) -> RenderVoicePlacement:
    return RenderVoicePlacement(
        script_scene_id=sid,
        source_voice=REF,
        start_ms=start,
        duration_ms=duration,
        storyboard_scene_ids=("sb1",),
    )


def _texts(text: str, ranges: list[tuple[int, int]]) -> list[str]:
    return [text[s:e] for s, e in ranges]


def test_sentences_japanese_and_english() -> None:
    ja = "縄文土器には焦げ跡が残る。煮炊きの証拠だ！本当？"
    assert _texts(ja, sentence_ranges(ja)) == [
        "縄文土器には焦げ跡が残る。",
        "煮炊きの証拠だ！",
        "本当？",
    ]
    en = "Pottery shows scorch marks. It was used for cooking! Really?"
    assert _texts(en, sentence_ranges(en)) == [
        "Pottery shows scorch marks.",
        "It was used for cooking!",
        "Really?",
    ]


def test_ascii_period_inside_numbers_and_closers() -> None:
    text = "Pi is 3.14 roughly. 「そうだ。」と言った"
    assert _texts(text, sentence_ranges(text)) == [
        "Pi is 3.14 roughly.",
        "「そうだ。」",
        "と言った",
    ]
    assert _texts("Wait... what?!", sentence_ranges("Wait... what?!")) == ["Wait...", "what?!"]


def test_wrap_uses_whitespace_when_present() -> None:
    text = "the quick brown fox jumps over"
    lines = _texts(text, wrap_lines(text, 0, len(text), 10))
    assert lines == ["the quick", "brown fox", "jumps over"]
    assert all(len(line) <= 10 for line in lines)


def test_wrap_breaks_long_words_and_cjk_by_character() -> None:
    assert _texts("abcdefghij", wrap_lines("abcdefghij", 0, 10, 4)) == ["abcd", "efgh", "ij"]
    ja = "あいうえおかきくけこさ"
    assert _texts(ja, wrap_lines(ja, 0, len(ja), 5)) == ["あいうえお", "かきくけこ", "さ"]


def test_long_japanese_sentence_without_whitespace_is_split_into_cues() -> None:
    ja = "縄文時代の人々は土器を使って木の実や魚を煮炊きしそれが定住生活を支える大きな力になった。"
    settings = _settings(max_chars_per_line=16, max_lines=2)
    ranges = cue_ranges(ja, settings)
    assert "".join(_texts(ja, ranges)) == ja
    assert all(e - s <= 32 for s, e in ranges)
    assert len(ranges) == 2


def test_long_english_sentence_respects_line_and_line_count() -> None:
    en = (
        "People of the Jomon period used pottery to boil nuts and fish, "
        "which helped them settle down in villages for generations."
    )
    settings = _settings(max_chars_per_line=20, max_lines=2)
    script = _script("en", [en, "Short.", "End."])
    cues = build_subtitle_cues(script, [_voice("s1", 0, 6000)], settings)
    texts = subtitle_display_texts(script, cues, settings)
    for text in texts:
        lines = text.split("\n")
        assert 1 <= len(lines) <= 2
        assert all(len(line) <= 20 for line in lines)
        assert not any(line != line.strip() for line in lines)
    # 語を割らない（単語の集合が保たれる）
    assert " ".join(t.replace("\n", " ") for t in texts).split() == en.split()


def test_cue_timing_is_proportional_contiguous_and_inside_the_window() -> None:
    script = _script("ja", ["あいう。あいうえおかきくけ。", "b。", "c。"])
    cues = build_subtitle_cues(script, [_voice("s1", 1000, 5000)], _settings())
    assert [(c.char_start, c.char_end) for c in cues] == [(0, 4), (4, 14)]
    assert [(c.start_ms, c.end_ms) for c in cues] == [(1000, 2428), (2428, 6000)]
    assert [c.cue_index for c in cues] == [0, 1]


def test_cues_across_voices_are_indexed_globally() -> None:
    script = _script("ja", ["一つ目。", "二つ目。", "三つ目。"])
    voices = [_voice("s1", 0, 1000), _voice("s2", 2000, 1000), _voice("s3", 4000, 500)]
    cues = build_subtitle_cues(script, voices, _settings())
    assert [(c.cue_index, c.script_scene_id, c.start_ms, c.end_ms) for c in cues] == [
        (0, "s1", 0, 1000),
        (1, "s2", 2000, 3000),
        (2, "s3", 4000, 4500),
    ]
    assert subtitle_display_texts(script, cues, _settings()) == ["一つ目。", "二つ目。", "三つ目。"]


def test_display_text_rewraps_exactly_the_cue_lines() -> None:
    ja = "あいうえおかきくけこさしすせそたちつてと。"
    settings = _settings(max_chars_per_line=8, max_lines=2)
    script = _script("ja", [ja, "x。", "y。"])
    cues = build_subtitle_cues(script, [_voice("s1", 0, 3000)], settings)
    assert [cue_display_text(script, c, settings) for c in cues] == [
        "あいうえおかきく\nけこさしすせそた",
        "ちつてと。",
    ]


def test_disabled_subtitles_produce_no_cues() -> None:
    script = _script("ja", ["あ。", "い。", "う。"])
    assert build_subtitle_cues(script, [_voice("s1", 0, 1000)], _settings(enabled=False)) == ()


def test_voice_too_short_for_its_cues() -> None:
    script = _script("ja", ["あ。い。う。", "b。", "c。"])
    with pytest.raises(VoiceTimelineOverflowError):
        build_subtitle_cues(script, [_voice("s1", 0, 2)], _settings())


def test_whitespace_only_tail_produces_no_empty_cue() -> None:
    assert cue_ranges("こんにちは。   ", _settings()) == [(0, 6)]
