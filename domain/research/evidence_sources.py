"""資料の種類と独立性（ADR-0038）。純粋。本文・LLM を見ずに決定的に決める。

- ``infer_source_kind``: ドメインから ``source_kind`` を決める（LLM に決めさせない）
- ``compute_origin_keys``: 同じ原典に由来する資料に同じ ``origin_key`` を付ける

``origin_key`` は近似で、次のどれかが同じなら同じ資料群にまとめる（推移的。union-find）:
(a) 同じ registrable domain（**保守側**: 同じサイトの別ページも同じ origin。独立を少なく数える）、
(b) 本文の内容 hash が同じ、(c) 正規化本文の主要部分の類似（転載）、(d) 資料が明示する原典 URL が
同じ。URL の重複判定は ``domain/research/urls.normalize_url``（唯一の定義）を使う。
"""

from __future__ import annotations

import hashlib
import re
from collections.abc import Sequence
from dataclasses import dataclass
from urllib.parse import urlsplit

from contracts.research_evidence import SourceKind
from domain.research.evidence_text import nfkc
from domain.research.urls import normalize_url

__all__ = [
    "NEAR_DUPLICATE_CONTAINMENT",
    "OriginInput",
    "compute_origin_keys",
    "host_of",
    "infer_source_kind",
    "is_fetchable_url",
    "registrable_domain",
]

_UNFETCHABLE_SUFFIXES = (
    ".pdf", ".jpg", ".jpeg", ".png", ".gif", ".webp", ".svg", ".mp4", ".mp3", ".zip",
    ".doc", ".docx", ".xls", ".xlsx", ".ppt", ".pptx",
)  # fmt: skip
#: 2 文字の国コード TLD の前の「第 2 レベル」（``co.jp`` ``ac.uk``）。公開サフィックスリストの近似
_SECOND_LEVEL_LABELS = frozenset(
    {"co", "com", "or", "ne", "ac", "go", "ed", "gr", "lg", "org", "gov", "edu", "net", "ad"}
)


def host_of(url: str) -> str:
    try:
        return (urlsplit(url).hostname or "").lower()
    except ValueError:
        return ""


def registrable_domain(host: str) -> str:
    """最後の 2 ラベル（``co.jp`` 等の第 2 レベルがあれば 3 ラベル）。先頭の ``www.`` は除く。"""
    host = host.lower().strip(".").removeprefix("www.")
    labels = host.split(".")
    if len(labels) <= 2 or all(label.isdigit() for label in labels):
        return host
    if len(labels[-1]) == 2 and labels[-2] in _SECOND_LEVEL_LABELS:
        return ".".join(labels[-3:])
    return ".".join(labels[-2:])


def is_fetchable_url(url: str) -> bool:
    """本文を取りに行く価値がある URL か（http(s) で、テキスト化できない拡張子でない）。"""
    try:
        parts = urlsplit(url)
    except ValueError:
        return False
    if parts.scheme not in ("http", "https") or not parts.hostname:
        return False
    return not parts.path.lower().endswith(_UNFETCHABLE_SUFFIXES)


_PRIMARY_HOSTS = frozenset(
    {"wikisource.org", "avalon.law.yale.edu", "perseus.tufts.edu", "gutenberg.org"}
)
_SCHOLARLY_HOSTS = frozenset(
    {"jstor.org", "doi.org", "arxiv.org", "cambridge.org", "oup.com", "cir.nii.ac.jp"}
)
_REFERENCE_HOSTS = frozenset(
    {"wikipedia.org", "wikimedia.org", "britannica.com", "kotobank.jp", "weblio.jp"}
)
_SCHOLARLY_SUFFIXES = (".edu", ".ac.jp", ".ac.uk", ".ac.kr", ".ac.cn")
_INSTITUTIONAL_SUFFIXES = (".gov", ".go.jp", ".lg.jp", ".mil", ".int", ".gov.uk", ".museum")
_INSTITUTIONAL_LABELS = ("museum", "archives", "hakubutsukan")


def _host_matches(host: str, names: frozenset[str]) -> bool:
    return any(host == name or host.endswith("." + name) for name in names)


def infer_source_kind(url: str) -> SourceKind:
    """ドメインから ``source_kind`` を決める（決定的。本文・LLM を見ない）。

    ドメインだけでは一次資料を見分けられないので、primary は明示した少数のホストに限る。
    分からないものは ``secondary``（強い主張の裏付けに数えない側）。
    """
    host = host_of(url)
    if not host:
        return SourceKind.SECONDARY
    if _host_matches(host, _PRIMARY_HOSTS):
        return SourceKind.PRIMARY
    if _host_matches(host, _SCHOLARLY_HOSTS) or host.endswith(_SCHOLARLY_SUFFIXES):
        return SourceKind.SCHOLARLY
    if host.endswith(_INSTITUTIONAL_SUFFIXES) or any(
        part.startswith(_INSTITUTIONAL_LABELS) for part in host.split(".")
    ):
        return SourceKind.INSTITUTIONAL
    if _host_matches(host, _REFERENCE_HOSTS):
        return SourceKind.REFERENCE
    return SourceKind.SECONDARY


#: 転載とみなす閾値。小さい方の本文の shingle のうち大きい方にも含まれる割合。**この値はここだけ**
NEAR_DUPLICATE_CONTAINMENT = 0.8
NEAR_DUPLICATE_MIN_SHINGLES = 15
SHINGLE_CHARS = 5

_CITE_MARKER = re.compile(
    r"(?:出典|転載元|引用元|原典|source|originally\s+(?:published|appeared)\s+(?:at|on|in)|"
    r"reprinted\s+from|via)\s*[:：]?\s*(https?://[^\s)）」』>\]]+)",
    re.I,
)
_URL_IN_TEXT = re.compile(r"https?://\S+")
_NON_TEXT = re.compile(r"[\W_]+", re.UNICODE)


@dataclass(frozen=True, slots=True)
class OriginInput:
    source_id: str
    url: str
    #: 確認済みの本文。本文が無い資料は ``None``（URL だけで判定する）
    body: str | None = None
    content_sha256: str | None = None


def _cited(body: str) -> frozenset[str]:
    return frozenset(
        normalize_url(m.group(1).rstrip(".,。、")) for m in _CITE_MARKER.finditer(body)
    )


def _shingles(body: str) -> frozenset[str]:
    folded = _NON_TEXT.sub("", _URL_IN_TEXT.sub("", nfkc(body)).casefold())
    if len(folded) < SHINGLE_CHARS:
        return frozenset()
    return frozenset(folded[i : i + SHINGLE_CHARS] for i in range(len(folded) - SHINGLE_CHARS + 1))


def _near_duplicate(a: frozenset[str], b: frozenset[str]) -> bool:
    small, large = (a, b) if len(a) <= len(b) else (b, a)
    if len(small) < NEAR_DUPLICATE_MIN_SHINGLES:
        return False
    return len(small & large) / len(small) >= NEAR_DUPLICATE_CONTAINMENT


def compute_origin_keys(sources: Sequence[OriginInput]) -> dict[str, str]:
    """``source_id -> origin_key``。入力の順序に依らない（代表は正規化 URL の最小）。"""
    n = len(sources)
    parent = list(range(n))

    def find(i: int) -> int:
        while parent[i] != i:
            parent[i] = parent[parent[i]]
            i = parent[i]
        return i

    def union(i: int, j: int) -> None:
        ri, rj = find(i), find(j)
        if ri != rj:
            parent[max(ri, rj)] = min(ri, rj)

    urls = [normalize_url(s.url) for s in sources]
    domains = [registrable_domain(host_of(s.url)) for s in sources]
    digests = [
        s.content_sha256
        or (hashlib.sha256(s.body.encode("utf-8")).hexdigest() if s.body is not None else None)
        for s in sources
    ]
    cited = [_cited(s.body) if s.body else frozenset[str]() for s in sources]
    shingles = [_shingles(s.body) if s.body else frozenset[str]() for s in sources]

    for i in range(n):
        for j in range(i + 1, n):
            if (
                (domains[i] != "" and domains[i] == domains[j])
                or (digests[i] is not None and digests[i] == digests[j])
                or urls[i] in cited[j]
                or urls[j] in cited[i]
                or bool(cited[i] & cited[j])
                or _near_duplicate(shingles[i], shingles[j])
            ):
                union(i, j)

    members: dict[int, list[int]] = {}
    for i in range(n):
        members.setdefault(find(i), []).append(i)
    keys: dict[str, str] = {}
    for group in members.values():
        representative = min(urls[i] for i in group)
        domain = domains[min(group, key=lambda i: urls[i])] or "unknown"
        key = f"{domain}#{hashlib.sha256(representative.encode('utf-8')).hexdigest()[:12]}"
        for i in group:
            keys[sources[i].source_id] = key
    return keys
