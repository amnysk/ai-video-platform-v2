"""Evidence の評価規則（ADR-0038 §2）。純粋関数。**評価器の提案を採用する前に必ず通す**。

評価器（LLM / Fake）の出力は**提案**である。ここが検査して確定する:

1. 参照の実在: 提案の claim_id・source_id・URL が、取得済みの資料（``FetchedContent.final_url``）に
   対応しなければ**提案ごと採用しない**（``insufficient``。評価器が作った URL・資料を根拠にしない）
2. 抜粋の実在: 抜粋が取得済み本文に無ければその link を捨てる。残す link の抜粋は本文側の実際の
   文字列に置き換える
3. 本文を完全に確認した資料（``fetched``）だけが根拠になる（切り詰め・失敗は根拠にならない）
4. 関連: 抜粋が claim と語を 1 つも共有しない（同じ言語のとき）``supports`` / ``qualifies`` は捨てる
5. 年代: 抜粋の年代表記が claim の年代と重ならない link は捨てる
6. 範囲・因果・最上級: 資料の範囲が claim より狭い、``causal`` の抜粋に因果の語が無い、
   ``superlative`` の抜粋に最上級の語が無い ``supports`` は ``qualifies`` に落とす
7. 独立性: 強い claim の ``supported`` は ``origin_key`` の異なる ``supports`` 2 件以上（うち 1 件は
   一次・学術・機関）。一次資料だけの ``causal`` は ``qualified`` まで
8. 異説: ``refutes`` と裏付けが併存すれば ``disputed``
9. 最終評価 = ``min(提案, コード評価)``（supported > qualified > disputed > insufficient）

コードの検出は評価を**弱める方向にだけ**作用する。``insufficient`` は使ってよい表現を持たない。
``disputed`` の表現はコードが作る（異説として紹介する形だけ）。
"""

from __future__ import annotations

from collections.abc import Collection, Iterable, Mapping
from dataclasses import dataclass

from contracts.research import ClaimImportance, ClaimKind
from contracts.research_evidence import (
    ALWAYS_STRONG_CLAIM_KINDS,
    AUTHORITATIVE_SOURCE_KINDS,
    MIN_INDEPENDENT_ORIGINS_FOR_STRONG_CLAIM,
    AssessmentProposal,
    ClaimAssessment,
    SourceFetchStatus,
    SourceKind,
    Stance,
)
from domain.research.evidence_ports import AssessClaim
from domain.research.evidence_text import (
    Language,
    clip,
    collapse_whitespace,
    detect_language,
    era_mismatch,
    extract_keywords,
    has_causal_terms,
    has_superlative_terms,
    keyword_hits,
    locate_excerpt,
    locator_at,
    quantifier_level,
    strength_terms,
)
from domain.research.urls import normalize_url

__all__ = [
    "ASSESSMENT_ORDER",
    "ClaimVerdict",
    "ConfirmedLink",
    "SourceFacts",
    "assess_claim",
    "assessment_rank",
    "check_sources_were_fetched",
    "default_strong",
    "determine_strong",
    "proposal_reference_error",
    "unassessed_verdict",
    "weaker_assessment",
]

#: 強い順。``min`` は後ろ（弱い）側を取る
ASSESSMENT_ORDER: tuple[ClaimAssessment, ...] = (
    ClaimAssessment.SUPPORTED,
    ClaimAssessment.QUALIFIED,
    ClaimAssessment.DISPUTED,
    ClaimAssessment.INSUFFICIENT,
)
_DISPLAY_EXCERPT_CHARS = 140


def assessment_rank(assessment: ClaimAssessment) -> int:
    """大きいほど強い（supported = 3 … insufficient = 0）。"""
    return len(ASSESSMENT_ORDER) - 1 - ASSESSMENT_ORDER.index(assessment)


def weaker_assessment(a: ClaimAssessment, b: ClaimAssessment) -> ClaimAssessment:
    return a if assessment_rank(a) <= assessment_rank(b) else b


def default_strong(kind: ClaimKind) -> bool:
    """種類だけで決まる ``strong``（``causal`` ``quantity`` ``superlative`` は常に強い）。"""
    return kind in ALWAYS_STRONG_CLAIM_KINDS


def determine_strong(kind: ClaimKind, importance: ClaimImportance, *, disputed: bool) -> bool:
    """``EvidenceClaim.strong`` の唯一の決定。種類で強い、または central で異説の疑いがある。"""
    return default_strong(kind) or (importance is ClaimImportance.CENTRAL and disputed)


@dataclass(frozen=True, slots=True)
class SourceFacts:
    """評価規則が見る資料の事実。本文は ``fetched``（完全に確認した）資料にだけある。"""

    source_id: str
    url: str
    fetch_status: SourceFetchStatus
    source_kind: SourceKind
    origin_key: str
    body: str | None = None
    language: Language = "en"


@dataclass(frozen=True, slots=True)
class ConfirmedLink:
    source_id: str
    stance: Stance
    locator: str
    #: **取得済み本文の実際の文字列**（提案された excerpt ではない）
    excerpt: str


@dataclass(frozen=True, slots=True)
class ClaimVerdict:
    claim_id: str
    assessed: bool
    assessment: ClaimAssessment
    strong: bool
    links: tuple[ConfirmedLink, ...]
    usable_expression: str
    reason: str
    unverified_points: tuple[str, ...]
    #: 提案とコード評価（``min`` の前）。テスト・監査用
    proposed_assessment: ClaimAssessment
    code_assessment: ClaimAssessment


def _t(language: Language, ja: str, en: str) -> str:
    return ja if language == "ja" else en


def unassessed_verdict(claim: AssessClaim, reason: str) -> ClaimVerdict:
    """評価しなかった（できなかった）claim。``insufficient`` で、使える表現を持たない。"""
    text = clip(reason, 500) or "not assessed"
    return ClaimVerdict(
        claim_id=claim.claim_id,
        assessed=False,
        assessment=ClaimAssessment.INSUFFICIENT,
        strong=default_strong(claim.kind),
        links=(),
        usable_expression="",
        reason=text,
        unverified_points=(clip(text, 300),),
        proposed_assessment=ClaimAssessment.INSUFFICIENT,
        code_assessment=ClaimAssessment.INSUFFICIENT,
    )


def proposal_reference_error(
    claim: AssessClaim, proposal: AssessmentProposal, sources: Mapping[str, SourceFacts]
) -> str | None:
    """提案の claim_id・source_id・URL が実在の取得結果に対応しなければ、その理由。

    評価器が存在しない資料や取得していない URL を作ったら、修復せず提案ごと採用しない。
    """
    if proposal.claim_id != claim.claim_id:
        return f"assessor answered {proposal.claim_id} for {claim.claim_id}"
    for link in proposal.links:
        source = sources.get(link.source_id)
        if source is None:
            return f"assessor cited an unknown source_id {link.source_id}"
        if normalize_url(link.source_url) != normalize_url(source.url):
            return f"assessor cited a URL that is not the fetched URL of {link.source_id}"
    return None


def _scope_is_narrower(claim_text: str, excerpt: str) -> bool:
    """資料の範囲が claim より狭い（全称の claim を部分・多数の記述で、多数を部分の記述で）。"""
    claim_level = quantifier_level(claim_text)
    excerpt_level = quantifier_level(excerpt)
    if claim_level >= 3:
        return excerpt_level < 3
    return claim_level == 2 and excerpt_level == 1


def _overstated(expression: str, excerpts: list[str]) -> bool:
    """表現が根拠の抜粋より強い（全称・因果・最上級の語や量化子の強さが抜粋に無い）。"""
    allowed: set[str] = set()
    top_level = 0
    for excerpt in excerpts:
        allowed |= strength_terms(excerpt)
        top_level = max(top_level, quantifier_level(excerpt))
    return bool(strength_terms(expression) - allowed) or quantifier_level(expression) > top_level


def _attributed(language: Language, excerpt: str) -> str:
    quoted = clip(excerpt, 300)
    return _t(
        language, f"資料によれば、「{quoted}」とされる。", f'According to a source: "{quoted}"'
    )


def _disputed_expression(language: Language, backing: str, refuting: str | None) -> str:
    a = clip(backing, _DISPLAY_EXCERPT_CHARS)
    if refuting is None:
        return _t(
            language,
            f"諸説あり、確定していない。「{a}」とする資料がある。",
            f'Accounts differ and the point is unsettled. One source says "{a}".',
        )
    r = clip(refuting, _DISPLAY_EXCERPT_CHARS)
    return _t(
        language,
        f"諸説あり、資料により見解が分かれる。「{a}」とする資料がある一方、「{r}」とする資料もある。",
        f'Accounts differ. One source says "{a}", while another says "{r}".',
    )


def assess_claim(
    claim: AssessClaim,
    proposal: AssessmentProposal,
    sources: Mapping[str, SourceFacts],
    *,
    language: Language,
) -> ClaimVerdict:
    """提案を検査して確定する（純粋・決定的）。"""
    reference_error = proposal_reference_error(claim, proposal, sources)
    if reference_error is not None:
        return unassessed_verdict(
            claim,
            _t(
                language,
                f"評価器の提案を採用しなかった（参照が取得結果に無い: {reference_error}）",
                f"assessor proposal rejected ({reference_error})",
            ),
        )

    notes: list[str] = []
    claim_language = detect_language(claim.text)
    claim_keywords = extract_keywords(claim.text)
    era_text = f"{claim.text} {claim.era or ''}"
    supports: list[ConfirmedLink] = []
    qualifies: list[ConfirmedLink] = []
    refutes: list[ConfirmedLink] = []
    seen: set[tuple[str, Stance, str]] = set()

    for proposed in proposal.links:
        source = sources[proposed.source_id]
        body = source.body if source.fetch_status is SourceFetchStatus.FETCHED else None
        located = locate_excerpt(body, proposed.excerpt) if body else None
        if body is None or located is None:
            notes.append(
                _t(
                    language,
                    f"{source.source_id}: 抜粋が本文を確認した資料に見つからないため除いた",
                    f"{source.source_id}: excerpt not found in a confirmed body; dropped",
                )
            )
            continue
        excerpt = located.text[:300]
        key = (source.source_id, proposed.stance, excerpt)
        if key in seen:
            continue
        seen.add(key)
        link = ConfirmedLink(
            source_id=source.source_id,
            stance=proposed.stance,
            locator=locator_at(body, located.start, source.language),
            excerpt=excerpt,
        )
        if link.stance is Stance.REFUTES:
            refutes.append(link)  # 反証は捨てない（捨てると評価が強くなる）
            continue
        if (
            claim_keywords
            and detect_language(excerpt) == claim_language
            and keyword_hits(claim_keywords, excerpt) == 0
        ):
            notes.append(
                _t(
                    language,
                    f"{source.source_id}: 抜粋が claim と語を共有しないため除いた",
                    f"{source.source_id}: excerpt shares no term with the claim; dropped",
                )
            )
            continue
        mismatch = era_mismatch(era_text, excerpt)
        if mismatch is not None:
            notes.append(
                _t(
                    language,
                    f"{source.source_id}: 年代が食い違うため根拠から除いた（{mismatch}）",
                    f"{source.source_id}: dates disagree with the claim; dropped ({mismatch})",
                )
            )
            continue
        if link.stance is Stance.SUPPORTS:
            demotion = _demotion_reason(claim, excerpt, language)
            if demotion is not None:
                notes.append(f"{source.source_id}: {demotion}")
                qualifies.append(
                    ConfirmedLink(link.source_id, Stance.QUALIFIES, link.locator, link.excerpt)
                )
                continue
            supports.append(link)
        else:
            qualifies.append(link)

    disputed_candidate = bool(refutes) or proposal.assessment is ClaimAssessment.DISPUTED
    strong = determine_strong(claim.kind, claim.importance, disputed=disputed_candidate)
    code = _code_assessment(claim, strong, supports, qualifies, refutes, sources, notes, language)
    final = weaker_assessment(proposal.assessment, code)
    if final is not proposal.assessment:
        notes.append(
            _t(
                language,
                f"評価器の提案 {proposal.assessment.value} を検査の結果 {final.value} にした",
                f"assessor proposed {proposal.assessment.value}; checks settled on {final.value}",
            )
        )

    backing = [link.excerpt for link in (*supports, *qualifies)]
    expression = _usable_expression(
        language=language,
        final=final,
        proposal=proposal,
        backing=backing,
        refuting=refutes[0].excerpt if refutes else None,
    )
    links = tuple(supports + qualifies + refutes)
    reason = collapse_whitespace(proposal.reason)
    if notes:
        joined = "; ".join(notes)
        reason = f"{reason} / {joined}" if final is proposal.assessment else joined
    points = tuple(dict.fromkeys(clip(p, 300) for p in (*notes, *proposal.unverified_points)))
    return ClaimVerdict(
        claim_id=claim.claim_id,
        assessed=True,
        assessment=final,
        strong=strong,
        links=links,
        usable_expression=expression,
        reason=clip(reason, 500) or final.value,
        unverified_points=points[:10],
        proposed_assessment=proposal.assessment,
        code_assessment=code,
    )


def _demotion_reason(claim: AssessClaim, excerpt: str, language: Language) -> str | None:
    if _scope_is_narrower(claim.text, excerpt):
        return _t(
            language,
            "資料の範囲が claim より狭い（全員・常に等に対し一部等）ため限定に落とした",
            "the source's scope is narrower than the claim; demoted to qualifies",
        )
    if claim.kind is ClaimKind.CAUSAL and not has_causal_terms(excerpt):
        return _t(
            language,
            "抜粋に因果を述べる語が無いため限定に落とした（因果の飛躍）",
            "the excerpt states no causation; demoted to qualifies",
        )
    if claim.kind is ClaimKind.SUPERLATIVE and not has_superlative_terms(excerpt):
        return _t(
            language,
            "抜粋に最上級・唯一性を述べる語が無いため限定に落とした",
            "the excerpt states no superlative or uniqueness; demoted to qualifies",
        )
    return None


def _code_assessment(
    claim: AssessClaim,
    strong: bool,
    supports: list[ConfirmedLink],
    qualifies: list[ConfirmedLink],
    refutes: list[ConfirmedLink],
    sources: Mapping[str, SourceFacts],
    notes: list[str],
    language: Language,
) -> ClaimAssessment:
    if refutes and (supports or qualifies):
        return ClaimAssessment.DISPUTED
    if not supports and not qualifies:
        if refutes:
            notes.append(_t(language, "反する資料のみで裏付けが無い", "only refuting sources"))
        return ClaimAssessment.INSUFFICIENT
    if not supports:
        return ClaimAssessment.QUALIFIED
    if strong:
        origins = {sources[link.source_id].origin_key for link in supports}
        if len(origins) < MIN_INDEPENDENT_ORIGINS_FOR_STRONG_CLAIM:
            notes.append(
                _t(
                    language,
                    f"強い主張だが独立した資料が {len(origins)} 件"
                    f"（{MIN_INDEPENDENT_ORIGINS_FOR_STRONG_CLAIM} 件要）",
                    f"strong claim with {len(origins)} independent origin(s); "
                    f"{MIN_INDEPENDENT_ORIGINS_FOR_STRONG_CLAIM} required",
                )
            )
            return ClaimAssessment.QUALIFIED
        if not any(
            sources[link.source_id].source_kind in AUTHORITATIVE_SOURCE_KINDS for link in supports
        ):
            notes.append(
                _t(
                    language,
                    "強い主張だが一次・学術・機関の資料による裏付けが無い",
                    "strong claim without a primary, scholarly or institutional source",
                )
            )
            return ClaimAssessment.QUALIFIED
    if claim.kind is ClaimKind.CAUSAL and all(
        sources[link.source_id].source_kind is SourceKind.PRIMARY for link in supports
    ):
        notes.append(
            _t(
                language,
                "一次資料の記述は当時の記録であり、因果を単独で確定しない",
                "a primary source records what was said then; it does not settle causation alone",
            )
        )
        return ClaimAssessment.QUALIFIED
    return ClaimAssessment.SUPPORTED


def _usable_expression(
    *,
    language: Language,
    final: ClaimAssessment,
    proposal: AssessmentProposal,
    backing: list[str],
    refuting: str | None,
) -> str:
    if final is ClaimAssessment.INSUFFICIENT:
        return ""
    if final is ClaimAssessment.DISPUTED:
        return _disputed_expression(language, backing[0], refuting)
    proposed = collapse_whitespace(proposal.usable_expression)
    if proposed and final is proposal.assessment and not _overstated(proposed, backing):
        return proposed
    return _attributed(language, backing[0])


def check_sources_were_fetched(
    source_urls: Iterable[str], fetched_final_urls: Collection[str]
) -> list[str]:
    """成果物の資料の URL のうち、実際に取得した最終 URL の集合に属さないもの（違反の一覧）。

    Handler はそう組むが、Handler を信用せずに実行器の側でも確かめる（URL を作らせない）。
    """
    allowed = {normalize_url(url) for url in fetched_final_urls}
    return [url[:80] for url in source_urls if normalize_url(url) not in allowed]
