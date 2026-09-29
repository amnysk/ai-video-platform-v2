"""``EvidenceHandler``: Evidence 種別の判断（ADR-0038 §1〜§3）。純粋・決定的。

共通の骨格（予約・予算・検索と取得・評価の呼び出し・生データ・成果物）は実行器が持つ。ここは:

- ``plan_searches`` / ``select_fetches``: ``evidence_planning`` の決定的な計画
- ``plan_assessments``: claim ごとに、本文を確認した資料から passage を選び、評価 1 回を計画する
  （passage が無い claim は評価しない＝評価器を呼ばない）
- ``synthesize``: 実行器が台帳を通して得た評価の**提案**を ``evidence_rules`` で検査・確定し、
  ``EvidenceArtifact`` を契約の validator を通して組む

完了の判断: 全 claim を「評価した」または「検索・取得したが関連する記述が無かった」なら
``complete``（評価が ``insufficient`` でもよい）。検索されなかった・本文を確認できなかった・
評価できなかった（評価器なし・枠切れ・失敗）claim があれば ``complete=False``（``partial``）。
"""

from __future__ import annotations

from collections.abc import Collection, Sequence
from typing import Any

from contracts.research import (
    RESEARCH_POLICY_VERSION,
    EvidenceResearchRequest,
    ResearchArtifactType,
    ResearchCoverage,
    ResearchKind,
    ResearchLanguage,
)
from contracts.research_evidence import (
    EVIDENCE_ARTIFACT_SCHEMA_VERSION,
    EVIDENCE_MAX_GAPS,
    ClaimAssessment,
    build_evidence_artifact,
    excerpt_digest,
)
from domain.research.errors import ResearchInputInvalidError
from domain.research.evidence_planning import (
    assess_claim_of,
    build_sources,
    claim_ids_of_step,
    claim_query_text,
    plan_evidence_searches,
    select_evidence_fetches,
    select_passages,
)
from domain.research.evidence_rules import ClaimVerdict, assess_claim, unassessed_verdict
from domain.research.evidence_text import clip
from domain.research.handlers import (
    AssessmentOutcome,
    AssessmentTask,
    FetchedSource,
    FetchTarget,
    HandlerOutput,
    ResearchSpec,
    SearchRound,
    SearchStep,
    SynthesisContext,
)

__all__ = ["EvidenceHandler"]


def _request(spec: ResearchSpec) -> EvidenceResearchRequest:
    if not isinstance(spec, EvidenceResearchRequest):
        raise ResearchInputInvalidError("EvidenceHandler needs an evidence research request")
    return spec


def _msg(language: ResearchLanguage, ja: str, en: str) -> str:
    return ja if language == "ja" else en


class EvidenceHandler:
    @property
    def kind(self) -> ResearchKind:
        return ResearchKind.EVIDENCE

    @property
    def artifact_type(self) -> ResearchArtifactType:
        return ResearchArtifactType.RESEARCH_EVIDENCE

    def plan_searches(self, spec: ResearchSpec, *, max_searches: int) -> tuple[SearchStep, ...]:
        return plan_evidence_searches(_request(spec).inputs.claim_inputs, max_searches=max_searches)

    def select_fetches(
        self,
        spec: ResearchSpec,
        rounds: Sequence[SearchRound],
        *,
        remaining: int,
        already_fetched: Collection[str],
    ) -> tuple[FetchTarget, ...]:
        _request(spec)
        return select_evidence_fetches(rounds, remaining=remaining, already_fetched=already_fetched)

    def plan_assessments(
        self,
        spec: ResearchSpec,
        rounds: Sequence[SearchRound],
        fetched: Sequence[FetchedSource],
    ) -> tuple[AssessmentTask, ...]:
        request = _request(spec)
        facts = [b.facts for b in build_sources(fetched, request.as_of)]
        tasks: list[AssessmentTask] = []
        for index, claim_input in enumerate(request.inputs.claim_inputs):
            claim = assess_claim_of(claim_input, index)
            passages = select_passages(claim, facts)
            if passages:
                tasks.append(AssessmentTask(task_id=claim.claim_id, claim=claim, passages=passages))
        return tuple(tasks)

    def synthesize(
        self,
        spec: ResearchSpec,
        ctx: SynthesisContext,
        rounds: Sequence[SearchRound],
        fetched: Sequence[FetchedSource],
    ) -> HandlerOutput:
        request = _request(spec)
        language = request.language
        built = build_sources(fetched, request.as_of)
        facts = {b.facts.source_id: b.facts for b in built}
        outcomes: dict[str, AssessmentOutcome] = {o.task_id: o for o in ctx.assessments}
        skipped = set(ctx.assessments_skipped)
        planned = {t.task_id for t in self.plan_assessments(spec, rounds, fetched)}

        searched = {
            c for r in rounds if r.results is not None for c in claim_ids_of_step(r.step.step_id)
        }
        hit_claims = {
            c
            for r in rounds
            if r.results and r.results.hits
            for c in claim_ids_of_step(r.step.step_id)
        }
        confirmed_claims = {c for b in built if b.facts.body for c in b.claim_ids}

        claims: list[dict[str, Any]] = []
        links: list[dict[str, Any]] = []
        gaps: list[dict[str, Any]] = []
        not_covered: list[str] = []
        warnings: list[str] = []
        for index, claim_input in enumerate(request.inputs.claim_inputs):
            claim = assess_claim_of(claim_input, index)
            cid = claim.claim_id
            missing: str | None = None
            verdict: ClaimVerdict
            outcome = outcomes.get(cid)
            if cid in planned and outcome is not None and outcome.proposal is not None:
                verdict = assess_claim(claim, outcome.proposal, facts, language=language)
                if not verdict.assessed:
                    missing = verdict.reason
                    warnings.append(clip(f"{cid}: assessor proposal rejected", 300))
            elif cid in planned:
                why = (
                    outcome.error
                    if outcome is not None and outcome.error
                    else ("skipped" if cid in skipped else "not assessed")
                )
                missing = _msg(
                    language,
                    f"評価できなかった（{why}）。評価していない主張は合格にしない",
                    f"not assessed ({why}); an unassessed claim is never accepted",
                )
                verdict = unassessed_verdict(claim, missing)
            elif cid not in searched:
                missing = _msg(
                    language,
                    "検索が実行されなかった（上限または検索の失敗）",
                    "no search was run (limit or search failure)",
                )
                verdict = unassessed_verdict(claim, missing)
            elif cid in hit_claims and cid not in confirmed_claims:
                missing = _msg(
                    language,
                    "候補はあったが本文を確認できなかった（取得の失敗・切り詰め・上限）",
                    "candidates found but no body text was confirmed (fetch failure or limit)",
                )
                verdict = unassessed_verdict(claim, missing)
            else:
                verdict = unassessed_verdict(
                    claim,
                    _msg(
                        language,
                        "検索したが関連する記述を本文に見つけられなかった",
                        "searched, but no relevant passage was found in a confirmed body",
                    ),
                )
            if missing is not None:
                not_covered.append(clip(f"{cid}: {missing}", 300))
            claims.append(_claim_row(claim_input, verdict))
            links.extend(_link_rows(verdict))
            if verdict.assessment is not ClaimAssessment.SUPPORTED:
                gaps.append(
                    {
                        "claim_id": cid,
                        "description": clip(
                            missing or f"{verdict.assessment.value}: {verdict.reason}", 300
                        ),
                        "suggested_query": clip(claim_query_text(claim_input), 200) or None,
                    }
                )

        for failure in ctx.failures:
            gaps.append(
                {"claim_id": None, "description": clip(failure, 300), "suggested_query": None}
            )
        artifact = build_evidence_artifact(
            request_id=ctx.request_id,
            as_of=ctx.as_of.isoformat(),
            claims=claims,
            sources=[b.row for b in built],
            links=links,
            gaps=gaps[:EVIDENCE_MAX_GAPS],
            policy_version=RESEARCH_POLICY_VERSION,
        )
        total = len(request.inputs.claim_inputs)
        return HandlerOutput(
            artifact_type=ResearchArtifactType.RESEARCH_EVIDENCE,
            schema_version=EVIDENCE_ARTIFACT_SCHEMA_VERSION,
            artifact=artifact,
            complete=not not_covered,
            coverage=ResearchCoverage(
                items_requested=total,
                items_covered=total - len(not_covered),
                not_covered=tuple(not_covered),
            ),
            warnings=tuple(warnings[:20]),
        )


def _claim_row(claim: Any, verdict: ClaimVerdict) -> dict[str, Any]:
    return {
        "claim_id": verdict.claim_id,
        "text": claim.claim_text,
        "kind": claim.kind.value,
        "era": claim.era,
        "region": claim.region,
        "importance": claim.importance.value,
        "strong": verdict.strong,
        "assessed": verdict.assessed,
        "assessment": verdict.assessment.value,
        "assessment_reason": verdict.reason,
        "usable_expression": verdict.usable_expression,
        "unverified_points": list(verdict.unverified_points),
    }


def _link_rows(verdict: ClaimVerdict) -> list[dict[str, Any]]:
    return [
        {
            "claim_id": verdict.claim_id,
            "source_id": link.source_id,
            "stance": link.stance.value,
            "locator": link.locator,
            "excerpt": link.excerpt,
            "excerpt_sha256": excerpt_digest(link.excerpt),
        }
        for link in verdict.links
    ]
