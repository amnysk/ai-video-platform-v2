"""タイトルの正規化（決定論）。"""

from __future__ import annotations

import re

#: 意味を持たない語（照合から除く）
STOPWORDS: frozenset[str] = frozenset(
    {
        "a", "an", "the", "of", "in", "on", "at", "to", "for", "and", "or", "but", "with",
        "by", "from", "as", "is", "are", "was", "were", "be", "been", "it", "its", "this",
        "that", "these", "those", "their", "they", "them", "his", "her", "he", "she", "we",
        "you", "your", "our", "what", "why", "how", "who", "when", "where", "which", "did",
        "do", "does", "really", "about", "into", "than", "then", "so", "not", "no",
    }
)  # fmt: skip

#: 語尾を落とす規則（接尾辞, 残す最小の語幹長）。上から順に最初の一致だけを適用する
_SUFFIXES: tuple[tuple[str, int], ...] = (("ing", 3), ("ed", 3), ("s", 3))
_WORD = re.compile(r"[a-z0-9]+")


def _stem(word: str) -> str:
    if word.endswith("ss"):
        return word
    for suffix, min_stem in _SUFFIXES:
        if word.endswith(suffix) and len(word) - len(suffix) >= min_stem:
            return word[: -len(suffix)]
    return word


def normalize_title(title: str) -> frozenset[str]:
    """小文字化・記号除去・stopword 除去・粗い語幹化をした語の集合。"""
    return frozenset(_stem(w) for w in _WORD.findall(title.lower()) if w not in STOPWORDS)


def jaccard(a: frozenset[str] | set[str], b: frozenset[str] | set[str]) -> float:
    if not a and not b:
        return 0.0
    return len(a & b) / len(a | b)
