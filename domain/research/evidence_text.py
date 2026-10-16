"""Evidence の判断に使う決定的な文字列処理（ADR-0038）。純粋（I/O・時刻・乱数なし。INV-6）。

ここは**検出の語彙と抽出**だけを持つ。何を弱めるかは ``evidence_rules.py``、検索語の組み立ては
``evidence_planning.py``、台本の照合は ``script_verification.py`` がここの関数を使う。語彙は
このファイルに 1 つだけ置く（AGENTS.md §8）。日本語と英語を扱う。

検出はすべて「評価を**弱める**方向」にだけ使う。語彙に無い言い回しは検出されず、見逃しは評価器と
契約の validator（最終防衛線）に委ねる。外部ページの本文は**データ**で、ここは部分一致・数え上げ
しかしない（本文の指示に従う経路を持たない）。
"""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass
from typing import Literal

Language = Literal["ja", "en"]

#: passage・抜粋の最大文字数（``contracts.research_evidence.EXCERPT_MAX_CHARS`` と同じ値。
#: ``tests/contract/test_research_evidence_contracts.py`` が一致を検査する）
PASSAGE_MAX_CHARS = 300

_QUOTE_TABLE = str.maketrans({"‘": "'", "’": "'", "“": '"', "”": '"', "–": "-", "—": "-", "−": "-"})


def nfkc(text: str) -> str:
    return unicodedata.normalize("NFKC", text)


def collapse_whitespace(text: str) -> str:
    return " ".join(text.split())


def clip(text: str, limit: int) -> str:
    """``limit`` 文字に収める（超えたら末尾を ``…`` にする）。"""
    text = collapse_whitespace(text)
    return text if len(text) <= limit else text[: max(limit - 1, 0)].rstrip() + "…"


def _fold(text: str) -> tuple[str, list[int]]:
    """空白を除き、引用符・ダッシュを畳んだ文字列と各文字の元位置（``text`` は NFKC 済み）。"""
    out: list[str] = []
    index: list[int] = []
    for i, ch in enumerate(text.translate(_QUOTE_TABLE)):
        if ch.isspace():
            continue
        out.append(ch)
        index.append(i)
    return "".join(out), index


@dataclass(frozen=True, slots=True)
class LocatedExcerpt:
    """本文側の実際の文字列（NFKC・空白を 1 つに畳む）と、NFKC 本文中の開始位置。"""

    text: str
    start: int


def locate_excerpt(body: str, excerpt: str) -> LocatedExcerpt | None:
    """``excerpt`` が ``body`` に**実在**すれば、本文側の実際の文字列と位置を返す。

    照合は空白・引用符の種類・全角半角（NFKC）を無視した部分一致。返すのは**本文の部分**であって
    提案された excerpt ではない（評価器が言い換えた抜粋を根拠として保存しない）。
    """
    body_n = nfkc(body)
    needle, _ = _fold(nfkc(excerpt))
    if not needle:
        return None
    haystack, index = _fold(body_n)
    at = haystack.find(needle)
    if at < 0:
        return None
    start = index[at]
    end = index[at + len(needle) - 1] + 1
    return LocatedExcerpt(collapse_whitespace(body_n[start:end]), start)


# ------------------------------------------------------------------ 言語


def detect_language(text: str) -> Language:
    """文字種で判定する。仮名があれば ja、漢字がラテン文字の 1/3 以上なら ja、それ以外は en。"""
    kana = han = latin = 0
    for ch in text:
        code = ord(ch)
        if 0x3040 <= code <= 0x30FF:
            kana += 1
        elif 0x4E00 <= code <= 0x9FFF:
            han += 1
        elif ch.isascii() and ch.isalpha():
            latin += 1
    if kana or (han and han * 3 >= latin):
        return "ja"
    return "en"


# ------------------------------------------------------------------ 文・段落


@dataclass(frozen=True, slots=True)
class TextUnit:
    """本文の 1 文。``paragraph`` / ``sentence`` は 1 始まり、``start`` は NFKC 本文中の位置。"""

    text: str
    paragraph: int
    sentence: int
    start: int


_PARAGRAPH = re.compile(r"[^\n]+")
_SENTENCE = re.compile(r".+?(?:[。！？]+|[.!?]+(?=\s|$)|$)", re.S)
_MIN_SENTENCE_CHARS = 4


def split_units(body: str) -> tuple[TextUnit, ...]:
    body_n = nfkc(body)
    units: list[TextUnit] = []
    paragraph = 0
    for para in _PARAGRAPH.finditer(body_n):
        if not para.group().strip():
            continue
        paragraph += 1
        sentence = 0
        for match in _SENTENCE.finditer(para.group()):
            raw = match.group()
            text = collapse_whitespace(raw)
            if len(text) < _MIN_SENTENCE_CHARS:
                continue
            sentence += 1
            lead = len(raw) - len(raw.lstrip())
            units.append(
                TextUnit(
                    text=text[:PASSAGE_MAX_CHARS],
                    paragraph=paragraph,
                    sentence=sentence,
                    start=para.start() + match.start() + lead,
                )
            )
    return tuple(units)


def locator_for(language: Language, paragraph: int, sentence: int) -> str:
    if language == "ja":
        return f"第{paragraph}段落・第{sentence}文"
    return f"paragraph {paragraph}, sentence {sentence}"


def locator_at(body: str, position: int, language: Language) -> str:
    """NFKC 本文中の位置が属する段落・文の位置表記（決定的）。"""
    best = TextUnit("", 1, 1, 0)
    for unit in split_units(body):
        if unit.start <= position:
            best = unit
        else:
            break
    return locator_for(language, best.paragraph, best.sentence)


# ------------------------------------------------------------------ キーワード

_KANJI_RUN = re.compile(r"[一-鿿々ヵヶ]{2,}")
_KATAKANA_RUN = re.compile(r"[ァ-ヴー]{2,}")
_LATIN_WORD = re.compile(r"[A-Za-z][A-Za-z'\-]{2,}")
_NUMBER = re.compile(r"(?<![\d,.])\d{3,4}(?![\d,])")

_STOP_LATIN = frozenset(
    [
        "the",
        "and",
        "was",
        "were",
        "that",
        "this",
        "with",
        "from",
        "for",
        "had",
        "has",
        "have",
        "are",
        "but",
        "not",
        "his",
        "her",
        "its",
        "they",
        "their",
        "which",
        "who",
        "whom",
        "when",
        "where",
        "into",
        "over",
        "after",
        "before",
        "than",
        "also",
        "been",
        "being",
        "will",
        "would",
        "could",
        "should",
        "about",
        "there",
        "these",
        "those",
        "then",
        "them",
        "she",
        "him",
        "our",
        "out",
        "any",
        "can",
        "may",
        "did",
        "does",
        "done",
    ]
)
_STOP_JA = frozenset(
    {"時代", "年代", "場合", "以上", "以下", "以降", "以前", "当時", "現在", "一般"}
)

MAX_KEYWORDS = 8


def extract_keywords(text: str, *, limit: int = MAX_KEYWORDS) -> tuple[str, ...]:
    """固有名詞・年代・キーワードを機械的に抜く（形態素解析なし。文字種の連なりで切る）。

    漢字・カタカナの 2 文字以上の連なり、3〜4 桁の数（年代）、3 文字以上の英単語（ストップワード
    を除く）。**出現順**で、大文字小文字を区別せずに重複を除く。
    """
    text_n = nfkc(text)
    found: list[tuple[int, str]] = []
    for pattern in (_KANJI_RUN, _KATAKANA_RUN, _NUMBER, _LATIN_WORD):
        for m in pattern.finditer(text_n):
            token = m.group()
            if pattern is _LATIN_WORD and token.casefold() in _STOP_LATIN:
                continue
            if pattern is _KANJI_RUN and token in _STOP_JA:
                continue
            found.append((m.start(), token))
    found.sort()
    seen: set[str] = set()
    keywords: list[str] = []
    for _, token in found:
        key = token.casefold()
        if key in seen:
            continue
        seen.add(key)
        keywords.append(token)
        if len(keywords) >= limit:
            break
    return tuple(keywords)


def keyword_hits(keywords: tuple[str, ...], text: str) -> int:
    folded = nfkc(text).casefold()
    return sum(1 for k in keywords if k.casefold() in folded)


# ------------------------------------------------------------------ 年代


@dataclass(frozen=True, slots=True)
class YearSpan:
    """西暦の範囲（紀元前は負）。単年は ``lo == hi``。"""

    lo: int
    hi: int

    def overlaps(self, other: YearSpan) -> bool:
        return self.lo <= other.hi and other.lo <= self.hi


_COUNTER_AFTER = (
    r"(?!\s?(?:人|名|騎|万|千|円|ドル|隻|門|丁|挺|個|頭|台|men\b|soldiers\b|troops\b|people\b|"
    r"ships\b|dollars\b|km\b|kg\b|%|percent\b|per\b))"
)
_CENTURY_JA = re.compile(r"(紀元前)?\s*(\d{1,2})\s*世紀")
_CENTURY_EN = re.compile(r"(\d{1,2})(?:st|nd|rd|th)[\s-]+century(?:\s+(BCE?))?", re.I)
_RANGE = re.compile(r"(?<!\d)(\d{3,4})\s*年?\s*(?:-|~|〜|から|to)\s*(\d{3,4})\s*年?(?!\d)")
_DECADE = re.compile(r"(?<!\d)(\d{3})0\s*(?:年代|s\b)")
_YEAR_JA = re.compile(r"(?<!\d)(紀元前|西暦)?\s*(\d{3,4})\s*年")
_YEAR_AD = re.compile(r"(?<![\d,.])(?:(AD|CE)\s*(\d{1,4})|(\d{1,4})\s*(AD|CE|BC|BCE))(?![\w])")
_YEAR_BARE = re.compile(r"(?<![\d,.])(1[0-9]{3}|20[0-9]{2})(?![\d,])" + _COUNTER_AFTER)


def _century(n: int, bc: bool) -> YearSpan:
    lo, hi = (n - 1) * 100 + 1, n * 100
    return YearSpan(-hi, -lo) if bc else YearSpan(lo, hi)


def find_year_spans(text: str) -> tuple[YearSpan, ...]:
    """年代表記（年・年代・世紀・範囲・紀元前）。和暦（慶長 5 年 等）は扱わない。"""
    t = nfkc(text)
    spans: list[YearSpan] = []
    consumed: list[tuple[int, int]] = []

    def free(m: re.Match[str]) -> bool:
        return not any(a <= m.start() < b or a < m.end() <= b for a, b in consumed)

    def take(m: re.Match[str], span: YearSpan) -> None:
        spans.append(span)
        consumed.append((m.start(), m.end()))

    for m in _CENTURY_JA.finditer(t):
        take(m, _century(int(m.group(2)), bool(m.group(1))))
    for m in _CENTURY_EN.finditer(t):
        take(m, _century(int(m.group(1)), bool(m.group(2))))
    for m in _RANGE.finditer(t):
        a, b = int(m.group(1)), int(m.group(2))
        if a <= b and free(m):
            take(m, YearSpan(a, b))
    for m in _DECADE.finditer(t):
        if free(m):
            base = int(m.group(1)) * 10
            take(m, YearSpan(base, base + 9))
    for m in _YEAR_JA.finditer(t):
        if free(m):
            year = int(m.group(2))
            take(m, YearSpan(-year, -year) if m.group(1) == "紀元前" else YearSpan(year, year))
    for m in _YEAR_AD.finditer(t):
        if not free(m):
            continue
        year, era = (int(m.group(2)), m.group(1)) if m.group(1) else (int(m.group(3)), m.group(4))
        take(m, YearSpan(-year, -year) if era.upper().startswith("BC") else YearSpan(year, year))
    for m in _YEAR_BARE.finditer(t):
        if free(m):
            year = int(m.group(1))
            take(m, YearSpan(year, year))
    return tuple(spans)


def era_mismatch(claim_text: str, excerpt: str) -> str | None:
    """claim の年代表記と抜粋の年代表記が**どれも重ならない**とき、その説明を返す。

    片方でも年代表記が無ければ ``None``（判断できない＝弱めない）。
    """
    claim_spans = find_year_spans(claim_text)
    excerpt_spans = find_year_spans(excerpt)
    if not claim_spans or not excerpt_spans:
        return None
    if any(c.overlaps(e) for c in claim_spans for e in excerpt_spans):
        return None

    def fmt(spans: tuple[YearSpan, ...]) -> str:
        return ", ".join(
            dict.fromkeys(str(s.lo) if s.lo == s.hi else f"{s.lo}-{s.hi}" for s in spans)
        )

    return f"claim {fmt(claim_spans)} vs excerpt {fmt(excerpt_spans)}"


# ------------------------------------------------------------------ 量化子・因果・最上級・異説

_UNIVERSAL = re.compile(
    r"すべて|全て|全員|全部|一人残らず|例外なく|ことごとく|あらゆる|常に|いつも|必ず|一度も|決して|"
    r"\b(?:all|every|everyone|everybody|always|never|entire|entirely|nobody|none|invariably|"
    r"without exception)\b",
    re.I,
)
_MAJORITY = re.compile(
    r"多くの|ほとんど|大半|大多数|しばしば|たいてい|概ね|おおむね|一般に|一般的に|"
    r"\b(?:many|most|mostly|often|usually|generally|frequently|largely|typically|majority|"
    r"commonly)\b|\bin general\b",
    re.I,
)
_PARTICULAR = re.compile(
    r"一部|少数|いくつか|数名|ある程度|まれに|稀に|ときに|場合もある|こともある|ものもある|"
    r"\b(?:some|sometimes|occasionally|several|certain|partly|partially|seldom|rarely|few)\b|"
    r"\ba few\b|\bin part\b|\bat times\b",
    re.I,
)

UNIVERSAL_LEVEL = 3
MAJORITY_LEVEL = 2
PARTICULAR_LEVEL = 1


def quantifier_level(text: str) -> int:
    """量化子の最大の強さ。3 = 全称、2 = 多数、1 = 部分、0 = なし。"""
    t = nfkc(text)
    if _UNIVERSAL.search(t):
        return UNIVERSAL_LEVEL
    if _MAJORITY.search(t):
        return MAJORITY_LEVEL
    if _PARTICULAR.search(t):
        return PARTICULAR_LEVEL
    return 0


_CAUSAL = re.compile(
    r"ため|ゆえ|原因|きっかけ|契機|招い|もたらし|せいで|影響で|ことにより|ことによって|"
    r"\b(?:because|due to|led to|leading to|lead to|caused|causing|causes|cause of|resulted in|"
    r"result of|as a result|therefore|thus|consequently|owing to|thanks to|prompted|triggered|"
    r"brought about|contributed to)\b",
    re.I,
)
_SUPERLATIVE = re.compile(
    r"最初|唯一|最大|最も|最古|最高|最小|最長|初めて|史上|世界一|日本一|空前|随一|"
    r"\b(?:first|only|largest|greatest|biggest|oldest|earliest|best|unique|sole|longest|"
    r"smallest|ever)\b",
    re.I,
)
_DISPUTE = re.compile(
    r"異説|他説|諸説|説もあ|とする説|議論がある|論争|見解が分かれ|"
    r"\b(?:disputed?|contested|debated|controvers\w*|accounts differ|alternative (?:view|theory)|"
    r"some (?:historians|scholars|sources))\b",
    re.I,
)
_HEDGE = re.compile(
    r"とされる|といわれ|と言われ|らしい|だろう|かもしれない|と伝えられ|によれば|おそらく|"
    r"\b(?:according to|reportedly|probably|possibly|perhaps|likely|may have|might have|"
    r"is said|are said|was said|were said|is thought|was thought|traditionally)\b",
    re.I,
)
_APPROXIMATE = re.compile(
    r"約|およそ|ほど|前後|余り|\b(?:about|around|approximately|roughly|nearly|almost|some)\b",
    re.I,
)

TERM_UNIVERSAL = "universal"
TERM_CAUSAL = "causal"
TERM_SUPERLATIVE = "superlative"


def has_causal_terms(text: str) -> bool:
    return bool(_CAUSAL.search(nfkc(text)))


def has_superlative_terms(text: str) -> bool:
    return bool(_SUPERLATIVE.search(nfkc(text)))


def has_dispute_terms(text: str) -> bool:
    return bool(_DISPUTE.search(nfkc(text)))


def is_hedged(text: str) -> bool:
    """伝聞・推量の形（「〜とされる」「according to」）で述べているか。"""
    return bool(_HEDGE.search(nfkc(text)))


def is_approximate(text: str) -> bool:
    return bool(_APPROXIMATE.search(nfkc(text)))


def strength_terms(text: str) -> frozenset[str]:
    """文が含む「強い断定」の種類。表現が資料より強くなっていないかの比較に使う。"""
    t = nfkc(text)
    found: set[str] = set()
    if _UNIVERSAL.search(t):
        found.add(TERM_UNIVERSAL)
    if _CAUSAL.search(t):
        found.add(TERM_CAUSAL)
    if _SUPERLATIVE.search(t):
        found.add(TERM_SUPERLATIVE)
    return frozenset(found)


_NUMBER_TOKEN = re.compile(r"(?<![\d.])\d+(?:[.,]\d+)*")


def numbers_in(text: str) -> frozenset[str]:
    """文中の数値（桁区切りを除いた文字列）。年も数量も同じに数える（強すぎる数値の検出用）。"""
    return frozenset(m.group().replace(",", "") for m in _NUMBER_TOKEN.finditer(nfkc(text)))


_QUOTE_SPANS = re.compile(r'"([^"]{4,})"|「([^」]{4,})」|『([^』]{4,})』')


def quoted_spans(text: str) -> tuple[str, ...]:
    """引用符で囲まれた発言（4 文字以上）。"""
    t = nfkc(text).translate(_QUOTE_TABLE)
    return tuple(next(g for g in m.groups() if g) for m in _QUOTE_SPANS.finditer(t))


def squash(text: str) -> str:
    """比較用: NFKC・casefold・空白と引用符の差を除く。"""
    folded, _ = _fold(nfkc(text).casefold())
    return folded.replace('"', "").replace("'", "")


__all__ = [
    "MAJORITY_LEVEL",
    "PARTICULAR_LEVEL",
    "PASSAGE_MAX_CHARS",
    "UNIVERSAL_LEVEL",
    "Language",
    "LocatedExcerpt",
    "TextUnit",
    "YearSpan",
    "clip",
    "collapse_whitespace",
    "detect_language",
    "era_mismatch",
    "extract_keywords",
    "find_year_spans",
    "has_causal_terms",
    "has_dispute_terms",
    "has_superlative_terms",
    "is_approximate",
    "is_hedged",
    "keyword_hits",
    "locate_excerpt",
    "locator_at",
    "locator_for",
    "nfkc",
    "numbers_in",
    "quantifier_level",
    "quoted_spans",
    "split_units",
    "squash",
    "strength_terms",
]
