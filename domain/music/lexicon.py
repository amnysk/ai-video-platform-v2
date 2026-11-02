"""要件導出の語彙表（ADR-0087 §5）。**キーワード → mood / 文化圏 / 時代 / 緊張の唯一の定義**。

- 語彙は固定。LLM は使わない。同じ文章は同じ要件になる
- ``key`` が ``MusicRequirements.matched_terms`` に出る固定語彙で、原文の語は決して出さない
- 表を変えたら ``BGM_SELECTION_RULE_VERSION`` を上げる（保存済みの決定は版に依らず再利用される）
- 並び順が優先順位（同点の mood / 時代は先に書いた方）
"""

from __future__ import annotations

import unicodedata
from dataclasses import dataclass

from contracts.music_selection import Culture, Tension

#: 緊張の強さの順（max を取るための序列）
TENSION_ORDER: dict[Tension, int] = {"low": 0, "medium": 1, "high": 2}

#: mood の既定（何も当たらないとき）と、同点のときの優先順
DEFAULT_MOOD = "calm"
MOOD_PRIORITY: tuple[str, ...] = (
    "tense",
    "epic",
    "mysterious",
    "solemn",
    "melancholic",
    "calm",
    "uplifting",
)

#: 文化圏の優先順（複数当たったら先のもの）
CULTURE_PRIORITY: tuple[Culture, ...] = ("japanese", "east_asian", "western")


@dataclass(frozen=True, slots=True)
class LexiconEntry:
    key: str
    terms: tuple[str, ...]
    mood: str | None = None
    tension: Tension | None = None
    culture: Culture | None = None
    era: str | None = None


LEXICON: tuple[LexiconEntry, ...] = (
    # ---- 緊張・戦い
    LexiconEntry(
        "war",
        ("戦", "合戦", "戦い", "戦争", "討ち", "攻め", "軍勢", "war", "battle", "siege", "army"),
        mood="tense",
        tension="high",
    ),
    LexiconEntry(
        "conflict",
        ("反乱", "の乱", "の変", "謀反", "暗殺", "rebellion", "revolt", "assassin", "coup"),
        mood="tense",
        tension="high",
    ),
    LexiconEntry(
        "epic",
        ("天下", "覇", "英雄", "帝国", "統一", "empire", "hero", "conquest", "legend"),
        mood="epic",
        tension="medium",
    ),
    # ---- 静けさ・日本の文化
    LexiconEntry(
        "temple",
        ("寺", "神社", "禅", "仏", "僧", "temple", "shrine", "zen", "monk", "buddh"),
        mood="calm",
        tension="low",
        culture="japanese",
    ),
    LexiconEntry(
        "tea",
        ("茶", "庭園", "花見", "桜", "tea ceremony", "garden"),
        mood="calm",
        tension="low",
        culture="japanese",
    ),
    LexiconEntry(
        "solemn",
        ("葬", "供養", "祈", "慰霊", "弔", "funeral", "memorial", "prayer", "ritual"),
        mood="solemn",
        tension="low",
    ),
    LexiconEntry(
        "melancholy",
        ("滅亡", "没落", "悲劇", "散り", "最期", "fall of", "tragedy", "lament", "doom"),
        mood="melancholic",
        tension="medium",
    ),
    LexiconEntry(
        "mystery",
        (
            "謎",
            "怪談",
            "妖怪",
            "伝説",
            "失われ",
            "秘密",
            "幻の",
            "mystery",
            "secret",
            "unsolved",
            "lost",
        ),
        mood="mysterious",
        tension="medium",
    ),
    LexiconEntry(
        "triumph",
        ("勝利", "繁栄", "栄華", "祭り", "victory", "triumph", "prosper", "festival"),
        mood="uplifting",
        tension="medium",
    ),
    # ---- 文化圏
    LexiconEntry(
        "japan",
        ("日本", "和風", "武士", "侍", "天皇", "幕府", "japan", "samurai", "shogun", "kyoto"),
        culture="japanese",
    ),
    LexiconEntry(
        "east_asia",
        ("中国", "唐代", "宋代", "漢王朝", "朝鮮", "china", "chinese", "korea", "dynasty"),
        culture="east_asian",
    ),
    LexiconEntry(
        "west",
        ("ローマ", "欧州", "ヨーロッパ", "中世", "騎士", "rome", "roman", "europe", "knight"),
        culture="western",
    ),
    # ---- 時代（日本史。文化圏も同時に japanese）
    LexiconEntry("era_heian", ("平安", "heian"), era="heian", culture="japanese"),
    LexiconEntry(
        "era_sengoku",
        ("戦国", "信長", "秀吉", "家康", "sengoku"),
        era="sengoku",
        culture="japanese",
    ),
    LexiconEntry("era_edo", ("江戸", "edo"), era="edo", culture="japanese"),
    LexiconEntry("era_meiji", ("明治", "維新", "meiji"), era="meiji", culture="japanese"),
    LexiconEntry("era_ancient", ("古代", "縄文", "弥生", "古墳", "ancient"), era="ancient"),
)


def normalize_text(text: str) -> str:
    """NFKC + casefold。全角・半角・大文字小文字の揺れを畳む。"""
    return unicodedata.normalize("NFKC", text).casefold()


def match_entries(text: str) -> tuple[LexiconEntry, ...]:
    """文章に当たった語彙（表の順・重複なし）。"""
    haystack = normalize_text(text)
    return tuple(
        entry for entry in LEXICON if any(normalize_text(term) in haystack for term in entry.terms)
    )


__all__ = [
    "CULTURE_PRIORITY",
    "DEFAULT_MOOD",
    "LEXICON",
    "MOOD_PRIORITY",
    "TENSION_ORDER",
    "LexiconEntry",
    "match_entries",
    "normalize_text",
]
