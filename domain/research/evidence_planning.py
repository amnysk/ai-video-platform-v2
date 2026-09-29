"""Evidence の claim 計画・検索語・取得候補・資料・passage（ADR-0038 §1）。決定的・純粋。

- claim id は依頼の ``claim_inputs`` の**並び順**から ``C-001`` と採番する（``claim_id_for`` だけ）
- 検索語は claim 本文から機械的に抜いた語だけで組む（URL・書誌・訳語を作らない）
- 検索の段 ID は ``c001-primary`` の形で、どの claim の検索かを ID から決定的に戻せる
- 資料（Source）は**取得の結果（``FetchedContent``）だけ**から作る。URL は ``final_url``。検索結果の
  snippet だけの資料は載せない（本文を確認していない資料を根拠の候補にしない）
- passage は本文を完全に確認した資料（``body_confirmed``）からだけ選ぶ
"""

from __future__ import annotations

import re
from collections.abc import Collection, Sequence
from dataclasses import dataclass
from datetime import datetime

from contracts.research import ClaimImportance, ClaimInput, ResearchLanguage
from contracts.research_evidence import (
    AUTHORITATIVE_SOURCE_KINDS,
    EVIDENCE_MAX_SOURCES,
    SourceFetchStatus,
    SourceKind,
    validate_http_url,
)
from domain.research.evidence_ports import AssessClaim, Passage
from domain.research.evidence_rules import SourceFacts, default_strong
from domain.research.evidence_sources import (
    OriginInput,
    compute_origin_keys,
    host_of,
    infer_source_kind,
    is_fetchable_url,
)
from domain.research.evidence_text import (
    clip,
    detect_language,
    extract_keywords,
    keyword_hits,
    locator_for,
    split_units,
)
from domain.research.handlers import FetchedSource, FetchTarget, SearchRound, SearchStep
from domain.research.ports import SearchQuery
from domain.research.urls import normalize_url

__all__ = [
    "EVIDENCE_SEARCH_MAX_RESULTS",
    "MAX_PASSAGES_PER_CLAIM",
    "BuiltSource",
    "assess_claim_of",
    "build_sources",
    "claim_id_for",
    "claim_ids_of_step",
    "claim_query_text",
    "plan_evidence_searches",
    "select_evidence_fetches",
    "select_passages",
]

#: 検索 1 回あたりに要求する結果件数
EVIDENCE_SEARCH_MAX_RESULTS = 5
MAX_PASSAGES_PER_SOURCE = 3
MAX_PASSAGES_PER_CLAIM = 12
_FALLBACK_QUERY_CHARS = 100
#: 異説を探す検索語（claim の言語で足す）
_DISPUTE_TERM: dict[str, str] = {"ja": "異説", "en": "disputed"}
_STEP_RE = re.compile(r"^c(\d{3})-(primary|dispute|xlang)$")


def claim_id_for(index: int) -> str:
    """``claim_inputs`` の 0 始まりの位置から claim id（``C-001`` 形式）。"""
    return f"C-{index + 1:03d}"


def claim_ids_of_step(step_id: str) -> tuple[str, ...]:
    """検索の段 ID が探した claim（Evidence の段でなければ空）。"""
    match = _STEP_RE.match(step_id)
    return (f"C-{match.group(1)}",) if match else ()


def assess_claim_of(claim: ClaimInput, index: int) -> AssessClaim:
    return AssessClaim(
        claim_id=claim_id_for(index),
        text=claim.claim_text,
        kind=claim.kind,
        importance=claim.importance,
        strong=default_strong(claim.kind),
        era=claim.era,
        region=claim.region,
    )


def claim_query_text(claim: ClaimInput, *, suffix: str = "") -> str:
    keywords = extract_keywords(claim.claim_text)
    base = " ".join(keywords) if keywords else clip(claim.claim_text, _FALLBACK_QUERY_CHARS)
    return f"{base} {suffix}".strip()


def _priority(claim: ClaimInput, index: int) -> tuple[int, int, int]:
    """小さいほど先。central > supporting、強い claim を優先、同順位は入力順。"""
    return (
        0 if claim.importance is ClaimImportance.CENTRAL else 1,
        0 if default_strong(claim.kind) else 1,
        index,
    )


def plan_evidence_searches(
    claims: Sequence[ClaimInput], *, max_searches: int
) -> tuple[SearchStep, ...]:
    """優先順の検索計画: (A) 全 claim の主検索 → (B) 強い・central な claim の異説探し →
    (C) 強い claim の他言語の検索（言語の絞り込みだけ変える。訳語は作らない）。

    ``max_searches`` に収まる分だけ返す（収まらない claim は検索されず、``partial`` の理由になる）。
    """
    order = sorted(range(len(claims)), key=lambda i: _priority(claims[i], i))
    steps: list[SearchStep] = []

    def add(index: int, text: str, language: ResearchLanguage, why: str) -> None:
        if len(steps) >= max_searches:
            return
        steps.append(
            SearchStep(
                step_id=f"c{index + 1:03d}-{why}",
                query=SearchQuery(
                    text=text,
                    kind="web",
                    max_results=EVIDENCE_SEARCH_MAX_RESULTS,
                    language=language,
                ),
                purpose=f"{claim_id_for(index)}:{why}",
            )
        )

    for i in order:
        add(i, claim_query_text(claims[i]), detect_language(claims[i].claim_text), "primary")
    for i in order:
        c = claims[i]
        if default_strong(c.kind) or c.importance is ClaimImportance.CENTRAL:
            language = detect_language(c.claim_text)
            add(i, claim_query_text(c, suffix=_DISPUTE_TERM[language]), language, "dispute")
    for i in order:
        c = claims[i]
        if default_strong(c.kind):
            other: ResearchLanguage = "en" if detect_language(c.claim_text) == "ja" else "ja"
            add(i, claim_query_text(c), other, "xlang")
    return tuple(steps)


def select_evidence_fetches(
    rounds: Sequence[SearchRound], *, remaining: int, already_fetched: Collection[str]
) -> tuple[FetchTarget, ...]:
    """取得候補（決定的）: (1) 各 claim に 1 件ずつ（特定の claim に枠が偏らない）、
    (2) 残りは全体の優先順（権威ある ``source_kind`` → 検索の順 → 結果の順）。
    テキスト化できない URL（PDF 等）・http(s) でない URL は取得しない。
    """
    fetched = {normalize_url(u) for u in already_fetched}
    candidates: dict[str, tuple[tuple[int, int, int], FetchTarget, set[str]]] = {}
    for step_no, round_ in enumerate(rounds):
        if round_.results is None or round_.step.query.kind != "web":
            continue
        claims = set(claim_ids_of_step(round_.step.step_id))
        for hit_no, hit in enumerate(round_.results.hits):
            key = normalize_url(hit.url)
            if key in fetched or not is_fetchable_url(hit.url):
                continue
            if key in candidates:
                candidates[key][2].update(claims)
                continue
            authority = 0 if infer_source_kind(hit.url) in AUTHORITATIVE_SOURCE_KINDS else 1
            target = FetchTarget(hit=hit, step_id=round_.step.step_id)
            candidates[key] = ((authority, step_no, hit_no), target, set(claims))

    ordered = sorted(candidates.items(), key=lambda kv: kv[1][0])
    chosen: list[str] = []
    seen_claims: set[str] = set()
    for key, (_, _, claims) in ordered:
        if len(chosen) >= remaining:
            break
        if claims - seen_claims:
            chosen.append(key)
            seen_claims |= claims
    for key, _ in ordered:
        if len(chosen) >= remaining:
            break
        if key not in chosen:
            chosen.append(key)
    return tuple(candidates[k][1] for k in chosen)


@dataclass(frozen=True, slots=True)
class BuiltSource:
    """成果物の資料 1 件（JSON の行）と、評価規則が見る事実。"""

    row: dict[str, object]
    facts: SourceFacts
    #: この資料を見つけた検索が探した claim
    claim_ids: tuple[str, ...]


def _status_of(fs: FetchedSource) -> SourceFetchStatus:
    content = fs.content
    if content.body_confirmed:
        return SourceFetchStatus.FETCHED
    if content.fetch_status == "failed":
        return SourceFetchStatus.FAILED
    return SourceFetchStatus.TRUNCATED  # 切り詰め・テキスト化できない・空本文


def _aware(value: datetime | None) -> datetime | None:
    return value if value is not None and value.tzinfo is not None else None


def build_sources(fetched: Sequence[FetchedSource], as_of: datetime) -> tuple[BuiltSource, ...]:
    """取得結果から資料を作る（決定的。同じ取得結果から同じ ``S-001`` の採番）。

    URL は**取得した最終 URL**（``final_url``）だけ。転送で同じ資料に着いた取得は 1 件にまとめる。
    """
    rows: list[tuple[dict[str, object], str | None, str | None, tuple[str, ...]]] = []
    index_by_url: dict[str, int] = {}
    for fs in fetched:
        content = fs.content
        final = content.final_url
        try:
            validate_http_url(final)
        except ValueError:
            continue
        key = normalize_url(final)
        claims = claim_ids_of_step(fs.target.step_id)
        if key in index_by_url:
            row, body, sha, known = rows[index_by_url[key]]
            rows[index_by_url[key]] = (
                row,
                body,
                sha,
                known + tuple(c for c in claims if c not in known),
            )
            continue
        status = _status_of(fs)
        body = content.text if status is SourceFetchStatus.FETCHED else None
        sha = content.content_sha256 if status is not SourceFetchStatus.FAILED else None
        if status is SourceFetchStatus.FETCHED and sha is None:
            status, body = (
                SourceFetchStatus.TRUNCATED,
                None,
            )  # 本文の hash が無い取得は確認済みにしない
        sample = body or f"{fs.target.hit.title} {fs.target.hit.snippet}"
        row: dict[str, object] = {
            "url": final,
            "title": clip(fs.target.hit.title, 300) or host_of(final) or "untitled",
            "language": detect_language(sample),
            "published_at": _iso(_aware(fs.target.hit.published_at)),
            "retrieved_at": _iso(_aware(content.fetched_at) or as_of),
            "content_sha256": sha,
            "fetch_status": status.value,
            "source_kind": infer_source_kind(final).value,
        }
        index_by_url[key] = len(rows)
        rows.append((row, body, sha, claims))

    rows = rows[:EVIDENCE_MAX_SOURCES]
    ids = [f"S-{n:03d}" for n in range(1, len(rows) + 1)]
    origins = compute_origin_keys(
        [
            OriginInput(source_id=sid, url=str(row[0]["url"]), body=row[1], content_sha256=row[2])
            for sid, row in zip(ids, rows, strict=True)
        ]
    )
    built: list[BuiltSource] = []
    for sid, (row, body, _sha, claims) in zip(ids, rows, strict=True):
        full = {"source_id": sid, "origin_key": origins[sid], **row}
        built.append(
            BuiltSource(
                row=full,
                facts=SourceFacts(
                    source_id=sid,
                    url=str(row["url"]),
                    fetch_status=SourceFetchStatus(str(row["fetch_status"])),
                    source_kind=SourceKind(str(row["source_kind"])),
                    origin_key=origins[sid],
                    body=body,
                    language="ja" if row["language"] == "ja" else "en",
                ),
                claim_ids=claims,
            )
        )
    return tuple(built)


def _iso(value: datetime | None) -> str | None:
    return None if value is None else value.isoformat()


def select_passages(claim: AssessClaim, sources: Sequence[SourceFacts]) -> tuple[Passage, ...]:
    """claim と語を共有する文を本文を確認した資料から選ぶ（決定的）。評価器の根拠はこれだけ。"""
    keywords = extract_keywords(claim.text)
    scored: list[tuple[int, int, int, Passage]] = []
    for order, source in enumerate(sources):
        if source.body is None or source.fetch_status is not SourceFetchStatus.FETCHED:
            continue
        per_source: list[tuple[int, int, Passage]] = []
        for unit in split_units(source.body):
            score = keyword_hits(keywords, unit.text)
            if score == 0:
                continue
            passage = Passage(
                source_id=source.source_id,
                source_url=source.url,
                locator=locator_for(source.language, unit.paragraph, unit.sentence),
                text=unit.text,
            )
            per_source.append((-score, unit.start, passage))
        per_source.sort(key=lambda item: (item[0], item[1]))
        scored.extend((s, order, start, p) for s, start, p in per_source[:MAX_PASSAGES_PER_SOURCE])
    scored.sort(key=lambda item: item[:3])
    return tuple(p for *_, p in scored[:MAX_PASSAGES_PER_CLAIM])
