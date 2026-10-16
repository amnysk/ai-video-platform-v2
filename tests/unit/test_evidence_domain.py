"""Evidence の純粋な規則（ADR-0038 §1〜§3）: 文字列処理・独立性・評価規則・計画。

評価器の出力は**提案**で、コードの検査は評価を弱める方向にだけ働く（min 規則）ことを、I/O 無しで
固定する。理由は docs/testing/research-evidence-rationale.md。
"""

from __future__ import annotations

from contracts.research import ClaimImportance, ClaimInput, ClaimKind
from contracts.research_evidence import (
    AssessmentProposal,
    ClaimAssessment,
    ProposedLink,
    SourceFetchStatus,
    SourceKind,
    Stance,
)
from domain.research.evidence_planning import (
    claim_ids_of_step,
    plan_evidence_searches,
)
from domain.research.evidence_ports import AssessClaim
from domain.research.evidence_rules import SourceFacts, assess_claim, weaker_assessment
from domain.research.evidence_sources import OriginInput, compute_origin_keys, infer_source_kind
from domain.research.evidence_text import era_mismatch, locate_excerpt, quantifier_level

BODY_A = "1543年、種子島にポルトガル人が漂着し、鉄砲が日本に伝えられたとされる。"
BODY_B = "鉄砲は1543年に伝来した。その後、1575年には長篠の戦いがあった。"
URL_A = "https://history-a.example/tanegashima"
URL_B = "https://history-b.example/nagashino"


def _claim(kind: ClaimKind = ClaimKind.YEAR, text: str = "鉄砲伝来は1543年") -> AssessClaim:
    return AssessClaim(
        claim_id="C-001",
        text=text,
        kind=kind,
        importance=ClaimImportance.SUPPORTING,
        strong=kind in (ClaimKind.CAUSAL, ClaimKind.QUANTITY, ClaimKind.SUPERLATIVE),
    )


def _facts(**overrides) -> dict[str, SourceFacts]:
    a = SourceFacts(
        "S-001", URL_A, SourceFetchStatus.FETCHED, SourceKind.SECONDARY, "o-a", BODY_A, "ja"
    )
    b = SourceFacts(
        "S-002", URL_B, SourceFetchStatus.FETCHED, SourceKind.SECONDARY, "o-b", BODY_B, "ja"
    )
    facts = {"S-001": a, "S-002": b}
    facts.update(overrides)
    return facts


def _proposal(
    *links: ProposedLink, assessment=ClaimAssessment.SUPPORTED, expr=""
) -> AssessmentProposal:
    return AssessmentProposal(
        claim_id="C-001",
        assessment=assessment,
        links=links,
        usable_expression=expr,
        reason="proposal",
        unverified_points=(),
    )


def _link(sid="S-001", url=URL_A, excerpt=BODY_A, stance=Stance.SUPPORTS) -> ProposedLink:
    return ProposedLink(source_id=sid, source_url=url, stance=stance, locator="p1", excerpt=excerpt)


# ------------------------------------------------------------------ 文字列


def test_excerpts_are_located_in_the_body_ignoring_width_and_spacing() -> None:
    located = locate_excerpt(BODY_A, "１５４３年、 種子島に")
    assert located is not None and located.text.startswith("1543年")
    assert locate_excerpt(BODY_A, "1542年に伝来") is None


def test_era_mismatch_and_quantifiers() -> None:
    assert era_mismatch("明治維新は1868年", "明治維新は1858年に成立") is not None
    assert era_mismatch("明治維新は1868年", "1868年に政権が移った") is None
    assert era_mismatch("明治維新", "1858年") is None  # 片方に年代が無ければ弱めない
    assert quantifier_level("すべての武将") > quantifier_level("一部の武将") > 0


def test_a_copied_body_shares_its_origin_and_other_sites_do_not() -> None:
    keys = compute_origin_keys(
        [
            OriginInput("S-001", URL_A, BODY_A),
            OriginInput("S-002", "https://farm.example/copy", BODY_A),
            OriginInput("S-003", URL_B, BODY_B),
        ]
    )
    assert keys["S-001"] == keys["S-002"] != keys["S-003"]
    assert infer_source_kind("https://www.kunaicho.go.jp/x") is SourceKind.INSTITUTIONAL
    assert infer_source_kind("https://blog.example/x") is SourceKind.SECONDARY


# ------------------------------------------------------------------ 評価規則


def test_the_final_assessment_is_never_stronger_than_the_proposal() -> None:
    verdict = assess_claim(
        _claim(),
        _proposal(_link(), assessment=ClaimAssessment.QUALIFIED, expr="x"),
        _facts(),
        language="ja",
    )
    assert verdict.assessment is ClaimAssessment.QUALIFIED
    assert (
        weaker_assessment(ClaimAssessment.SUPPORTED, ClaimAssessment.DISPUTED)
        is ClaimAssessment.DISPUTED
    )


def test_a_proposal_citing_an_unfetched_url_or_unknown_source_is_not_adopted() -> None:
    for link in (_link(url="https://made-up.example/x"), _link(sid="S-009")):
        verdict = assess_claim(_claim(), _proposal(link), _facts(), language="ja")
        assert not verdict.assessed and verdict.assessment is ClaimAssessment.INSUFFICIENT


def test_a_fabricated_excerpt_is_dropped() -> None:
    verdict = assess_claim(
        _claim(),
        _proposal(_link(excerpt="1543年に火縄銃が大量生産された")),
        _facts(),
        language="ja",
    )
    assert verdict.assessment is ClaimAssessment.INSUFFICIENT and verdict.links == ()


def test_a_truncated_source_is_never_a_basis() -> None:
    facts = _facts(
        **{
            "S-001": SourceFacts(
                "S-001", URL_A, SourceFetchStatus.TRUNCATED, SourceKind.SECONDARY, "o-a", BODY_A
            )
        }
    )
    verdict = assess_claim(_claim(), _proposal(_link()), facts, language="ja")
    assert verdict.assessment is ClaimAssessment.INSUFFICIENT


def test_a_causal_claim_without_causal_wording_is_only_qualified() -> None:
    claim = _claim(ClaimKind.CAUSAL, "鉄砲伝来が長篠の戦いの勝敗を決めた")
    verdict = assess_claim(
        claim,
        _proposal(_link("S-002", URL_B, BODY_B.split("。")[1] + "。")),
        _facts(),
        language="ja",
    )
    assert verdict.assessment in (ClaimAssessment.QUALIFIED, ClaimAssessment.INSUFFICIENT)
    assert verdict.assessment is not ClaimAssessment.SUPPORTED


def test_a_refutation_alongside_support_makes_the_claim_disputed_with_a_code_expression() -> None:
    dissent = "鉄砲伝来は一般に1543年とされるが、1542年とする説もあり、年代には異説がある。"
    facts = _facts(
        **{
            "S-002": SourceFacts(
                "S-002",
                URL_B,
                SourceFetchStatus.FETCHED,
                SourceKind.SECONDARY,
                "o-b",
                dissent,
                "ja",
            )
        }
    )
    verdict = assess_claim(
        _claim(),
        _proposal(_link(), _link("S-002", URL_B, dissent, Stance.REFUTES), expr="必ず1543年"),
        facts,
        language="ja",
    )
    assert verdict.assessment is ClaimAssessment.DISPUTED
    assert verdict.usable_expression.startswith("諸説あり")


def test_an_overstated_proposed_expression_is_replaced_by_an_attributed_one() -> None:
    verdict = assess_claim(
        _claim(),
        _proposal(_link(), expr="鉄砲はすべての大名に常に伝えられた"),
        _facts(),
        language="ja",
    )
    assert verdict.assessment is ClaimAssessment.SUPPORTED
    assert verdict.usable_expression.startswith("資料によれば")


# ------------------------------------------------------------------ 計画


def test_search_planning_is_deterministic_and_step_ids_name_their_claim() -> None:
    claims = (
        ClaimInput(
            claim_text="鉄砲伝来は1543年",
            kind=ClaimKind.YEAR,
            importance=ClaimImportance.SUPPORTING,
        ),
        ClaimInput(
            claim_text="長篠の戦いで鉄砲が最大の役割",
            kind=ClaimKind.SUPERLATIVE,
            importance=ClaimImportance.CENTRAL,
        ),
    )
    steps = plan_evidence_searches(claims, max_searches=10)
    assert steps == plan_evidence_searches(claims, max_searches=10)
    assert steps[0].step_id == "c002-primary"  # central かつ強い claim が先
    assert claim_ids_of_step("c001-primary") == ("C-001",)
    assert len(plan_evidence_searches(claims, max_searches=1)) == 1
    assert all("http" not in s.query.text for s in steps)  # URL を作らない
