"""historian_agent_v1 → FactCheckReport（ADR-0047 §2/§3、INV-58 / INV-60）。

台本から**検証できる主張**を LLM に ≤12 件挙げさせ（plan の 1 回で ``search_historical_facts`` の
入力にする）、tool の評価を**そのまま**報告にする。LLM は評価も根拠も作れない:

- 主張は決定論で検査する（``ClaimInput`` で不正・重複を除き、12 件で切る。余分な欄は転送しない）
- ``assessment`` は tool の語（supported / qualified / disputed / insufficient）をそのまま写す。
  知らない語は ``insufficient``（推測で写さない）。数値の信頼度は作らない
- 根拠は ``tool_call:<id>`` と tool が返した資料 ID（``evidence:<source_id>``）だけ
- tool が使えない・未完・失敗のときは、挙げた主張を全て ``insufficient`` で報告する
- **台本は書き換えない**（成果物は report だけ。提案の自動適用もしない）。台本が無いときは
  LLM も tool も呼ばず ``blocked_missing_input`` の report を返す（quality と同じ）

``fetch_sources`` は使わない: 資料の一覧には Research の依頼 ID が要るが、それは
``search_historical_facts`` の**結果**にしか無く、結果から次の tool 要求を作ると 1 往復の
規則（INV-60）を破る。資料の ID は検索結果の ``source_ids`` から ``evidence:`` として引く。
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from pydantic import ValidationError

from contracts.agent_tools import ToolEnvelope, ToolId, ToolStatus, tool_ref
from contracts.agent_tools_research import ClaimAssessmentView, SearchHistoricalFactsOutput
from contracts.agentic import ClaimAssessment, FactCheckedClaim, FactCheckReport
from contracts.intervention import DecisionCandidate, DecisionRecordPayload, RationaleKind
from contracts.research import MAX_CLAIMS_PER_REQUEST, ClaimInput, normalize_claim_text
from domain.agents.handlers._support import clip, document, validated
from domain.agents.handlers.tool_use import (
    REASON_INVALID_INPUT,
    ToolRound,
    plan_and_execute,
)
from domain.agents.runtime import AgentInput, AgentOutput
from domain.agents.tool_gateway import ToolGateway

MAX_CLAIMS = MAX_CLAIMS_PER_REQUEST
MAX_EVIDENCE_REFS = 10
MAX_LIMITATIONS = 20
REASON_NO_VALID_CLAIMS = "no_valid_claims"
BLOCKED_NOTE = "blocked_missing_input"
BLOCKED_LIMITATION = "blocked_missing_input:script"

_SEARCH_REF = tool_ref(ToolId.SEARCH_HISTORICAL_FACTS, 1)

TASK = (
    "List up to 12 checkable historical claims that the script actually makes (a year, person, "
    "place, event, quantity, comparison, causal or superlative claim). For each give claim_text "
    "(as the script states it), kind, importance (central or supporting) and, when the script "
    "says so, era and region. Do not add claims the script does not make and do not judge them "
    f"yourself. Then request {_SEARCH_REF} once with those claims."
)


def _has_script(script: Mapping[str, Any]) -> bool:
    return bool(script.get("scenes"))


def historian_needs_llm(documents: Mapping[str, Mapping[str, Any]]) -> bool:
    """台本が無ければ LLM も tool も呼ばない（予約も取らない）。判定できなければ ``True``。"""
    try:
        return _has_script(dict(documents.get("script", {})))
    except Exception:
        return True


def _condensed_script(script: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "title": script.get("title"),
        "hook": script.get("hook"),
        "language": script.get("language"),
        "scenes": [
            {"scene_id": s.get("scene_id"), "narration": s.get("narration")}
            for s in script.get("scenes", ())
            if isinstance(s, Mapping)
        ],
    }


class _ClaimPolicy:
    """LLM が挙げた主張の入力を決定論で絞る（``InputPolicy``）。数えた事実は limitations に残す。"""

    def __init__(self, language: Any) -> None:
        self._language = language if language in ("ja", "en") else None
        self.invalid = 0
        self.duplicate = 0
        self.truncated = 0

    def __call__(self, ref: str, tool_input: dict[str, Any]) -> dict[str, Any] | str:
        raw = tool_input.get("claims")
        if not isinstance(raw, list):
            return REASON_INVALID_INPUT
        claims: list[dict[str, Any]] = []
        seen: set[str] = set()
        for item in raw:
            if not isinstance(item, dict):
                self.invalid += 1
                continue
            # 余分な欄（assessment 等）は転送しない: 評価は tool だけが決める
            fields = {k: v for k, v in item.items() if k in ClaimInput.model_fields}
            try:
                claim = ClaimInput.model_validate(fields)
            except ValidationError:
                self.invalid += 1
                continue
            key = normalize_claim_text(claim.claim_text)
            if key in seen:
                self.duplicate += 1
                continue
            seen.add(key)
            if len(claims) >= MAX_CLAIMS:
                self.truncated += 1
                continue
            claims.append(claim.model_dump(mode="json"))
        if not claims:
            return REASON_NO_VALID_CLAIMS
        sent: dict[str, Any] = {"claims": claims}
        if self._language is not None:
            sent["language"] = self._language  # 言語は台本が決める（LLM に選ばせない）
        return sent

    def limitations(self) -> list[str]:
        out: list[str] = []
        if self.invalid or self.duplicate:
            out.append(
                f"{self.invalid} invalid and {self.duplicate} duplicate claim(s) were dropped "
                "before the research tool was called."
            )
        if self.truncated:
            out.append(
                f"{self.truncated} claim(s) beyond the limit of {MAX_CLAIMS} were truncated."
            )
        return out


def _assessment(view: ClaimAssessmentView) -> tuple[ClaimAssessment, str | None]:
    try:
        return ClaimAssessment(view.assessment), None
    except ValueError:
        return ClaimAssessment.INSUFFICIENT, "unrecognized assessment from the tool; not mapped"


def _from_tool(envelope: ToolEnvelope) -> list[FactCheckedClaim]:
    output = SearchHistoricalFactsOutput.model_validate(envelope.data)
    claims: list[FactCheckedClaim] = []
    for view in output.claims:
        assessment, problem = _assessment(view)
        refs = [f"tool_call:{envelope.tool_call_id}"]
        if assessment is not ClaimAssessment.INSUFFICIENT:
            refs += [f"evidence:{sid}" for sid in view.source_ids]
        reason = problem or view.assessment_reason
        claims.append(
            FactCheckedClaim(
                claim=clip(view.claim_text, 500),
                assessment=assessment,
                evidence_refs=tuple(refs[:MAX_EVIDENCE_REFS]),
                note=clip(reason, 300) if reason else None,
            )
        )
    return claims


def _unverified(
    texts: list[str], note: str, envelope: ToolEnvelope | None
) -> list[FactCheckedClaim]:
    refs = (f"tool_call:{envelope.tool_call_id}",) if envelope is not None else ()
    return [
        FactCheckedClaim(
            claim=clip(t, 500),
            assessment=ClaimAssessment.INSUFFICIENT,
            evidence_refs=refs,
            note=clip(note, 300),
        )
        for t in texts
    ]


def _claims(tool_round: ToolRound) -> list[FactCheckedClaim]:
    if not tool_round.envelopes:
        return []
    envelope = tool_round.envelopes[0]
    sent = [str(c["claim_text"]) for c in tool_round.executed_inputs[0]["claims"]]
    if envelope.status is not ToolStatus.OK:
        return _unverified(sent, f"not verified: tool status {envelope.status.value}", envelope)
    checked = _from_tool(envelope)
    returned = {normalize_claim_text(c.claim) for c in checked}
    missing = [t for t in sent if normalize_claim_text(t) not in returned]
    checked += _unverified(missing, "not returned by the research tool", envelope)
    return checked[:MAX_CLAIMS]


def _decision(
    report: FactCheckReport, tool_round: ToolRound, limitations: list[str]
) -> DecisionRecordPayload:
    counts = {a: sum(1 for c in report.claims if c.assessment is a) for a in ClaimAssessment}
    summary = f"{len(report.claims)} claim(s): " + ", ".join(
        f"{a.value} {n}" for a, n in counts.items()
    )
    return DecisionRecordPayload(
        decision_type="fact_check",
        question="Which claims in the script could be verified against sources?",
        candidates=(DecisionCandidate(id="fact_check_report", summary=summary),),
        selected_candidate="fact_check_report",
        rationale_kind=RationaleKind.RULE,
        rationale=(
            "Assessments are passed through from the research tool; no confidence was added and "
            "the script was not edited."
        ),
        evidence_refs=tuple(dict.fromkeys([*tool_round.citations, *tool_round.evidence_refs])),
        uncertainties=tuple(limitations),
    )


def _blocked(agent_input: AgentInput) -> AgentOutput:
    report = FactCheckReport(episode_id=agent_input.episode_id, limitations=(BLOCKED_LIMITATION,))
    decision = DecisionRecordPayload(
        decision_type="fact_check",
        question="Which claims in the script could be verified against sources?",
        candidates=(DecisionCandidate(id=BLOCKED_NOTE, summary="missing: script"),),
        rationale_kind=RationaleKind.RULE,
        rationale="The script is missing, so no claim was extracted and nothing was checked.",
        constraints=("script",),
    )
    return AgentOutput(artifact=report, decision=decision, outcome="produced", note=BLOCKED_NOTE)


async def historian_agent_v1(agent_input: AgentInput, gateway: ToolGateway) -> AgentOutput:
    script = dict(document(agent_input, "script"))
    if not _has_script(script):
        return _blocked(agent_input)
    brief = dict(document(agent_input, "user_brief"))
    policy = _ClaimPolicy(script.get("language"))
    tool_round = await plan_and_execute(
        gateway,
        task=TASK,
        data={"script": _condensed_script(script), "theme": brief.get("theme")},
        tool_refs={_SEARCH_REF},
        policy=policy,
    )
    limitations = [*policy.limitations(), *tool_round.notes]
    if not tool_round.envelopes and not tool_round.unavailable:
        limitations.append("no checkable claim was sent to the research tool.")
    report = validated(
        FactCheckReport,
        {
            "episode_id": agent_input.episode_id,
            "claims": [c.model_dump() for c in _claims(tool_round)],
            "limitations": [clip(lim, 200) for lim in limitations][:MAX_LIMITATIONS],
        },
        "fact_check_report",
    )
    return AgentOutput(
        artifact=report, decision=_decision(report, tool_round, limitations), outcome="produced"
    )
