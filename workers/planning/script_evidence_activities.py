"""台本の Evidence 照合の Activity（ADR-0038 §B6。opt-in・既定 OFF）。

``SCRIPT_EVIDENCE_ENABLED`` のときだけ ``workers/planning/research_wiring.py`` が組んで登録する。
OFF のときこのモジュールは読み込まれない。

``script_evidence_check`` の手順（結果は**助言**。どこで止まっても例外にせず結果を返す）:

1. 台本（本番の Artifact）を読み戻し、sha256 を照合してから契約で読む
2. 主張の候補: 決定的な網（``detect_candidates``）∪ ``ClaimExtractor``（``fake`` だけ。
   実 LLM は配線しない）。候補が無ければ依頼しない（``no_claims``）
3. Evidence を **Gateway 経由で**依頼する（冪等キーは Episode と台本の sha256。
   予算の門・鮮度キャッシュは Gateway が持つ）。聴き手の説明は Strategy profile の
   ``audience_description`` を読むだけ（Strategy のコードは変えない）。``episode_id`` は
   research 側に参照として残る（追跡）
4. ``queued`` なら ``ResearchWorkflow`` を起動し、``SCRIPT_EVIDENCE_WAIT_SECONDS`` まで
   DB を読んで待つ（heartbeat を送る）。上限を越えたら ``timeout``（Research は止めない）
5. ``completed`` のときだけ ``ScriptVerifier`` で照合し、照合結果の参照を返す（research の
   成果物として Evidence の依頼が所有する）。``completed`` でなければ「調査なし」（INV-37）

結論が ``failed`` / ``insufficient`` でも Episode は止めない。自動の書き直し
（Codex の追加呼び出し）は移植していない（ADR-0038 §B6）。
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable, Sequence
from datetime import UTC, datetime

from pydantic import ValidationError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from temporalio import activity

from contracts.artifact_refs import ArtifactDigestRef
from contracts.artifacts import ScriptArtifact, parse_script_artifact
from contracts.research import (
    CLAIM_TEXT_MAX_CHARS,
    MAX_CLAIMS_PER_REQUEST,
    ClaimImportance,
    ClaimInput,
    ClaimKind,
    EvidenceResearchInputs,
    EvidenceResearchSubmit,
    ResearchArtifactType,
    ResearchKind,
    ResearchResult,
    ResearchStatus,
    TimeWindow,
    normalize_claim_text,
)
from contracts.research_evidence import ScriptUnitInput, ScriptVerificationRequest
from contracts.topic_planning import STRATEGY_PROFILES, StrategyProfile
from domain.artifact.hashing import canonical_json_bytes, sha256_hex
from domain.research.entities import ResearchRequest
from domain.research.evidence_ports import CandidateSentence, ClaimExtractor
from domain.research.script_verification import detect_candidates
from domain.research.status import RESEARCH_TERMINAL_STATUSES
from infrastructure.db.repositories import EpisodeRepository, TopicPlanRepository
from infrastructure.research.gateway import ResearchGateway
from infrastructure.research.verification import ScriptVerifier
from infrastructure.storage.artifact_store import ArtifactStore
from infrastructure.temporal.research_starter import ResearchWorkflowStarter
from workers.planning.script_evidence import (
    SCRIPT_EVIDENCE_CHECK,
    SCRIPT_EVIDENCE_POLL_SECONDS,
    SCRIPT_EVIDENCE_WAIT_SECONDS,
    EvidenceCheckOutcome,
    ScriptEvidenceOutcome,
    ScriptEvidenceRequest,
)

__all__ = ["ScriptEvidenceActivities", "claims_from_candidates", "script_units"]

logger = logging.getLogger(__name__)

REQUESTER = "script_writer"
#: 依頼の対象期間。Evidence の ``request_hash`` は ``as_of`` を含めず ``time_window`` を含むので、
#: 固定の窓にして同じ台本の依頼を同じ意味にする（再実行・翌日の再照合でも冪等キーと食い違わない）。
#: 資料の公開日で絞らない（歴史の主張は古い資料にも拠る）
EVIDENCE_TIME_WINDOW = TimeWindow(
    start=datetime(1900, 1, 1, tzinfo=UTC), end=datetime(2100, 1, 1, tzinfo=UTC)
)

#: 候補の徴候 → 主張の種類（強い方を先に見る）
_KIND_BY_TRIGGER: tuple[tuple[str, ClaimKind], ...] = (
    ("causal", ClaimKind.CAUSAL),
    ("superlative", ClaimKind.SUPERLATIVE),
    ("universal", ClaimKind.COMPARISON),
    ("quantity", ClaimKind.QUANTITY),
    ("year", ClaimKind.YEAR),
)
_CENTRAL_UNITS = frozenset({"title", "hook"})


def script_units(script: ScriptArtifact) -> list[tuple[str, str]]:
    """照合の単位（``unit_id`` と最終文面）。

    ``unit_id`` は ``contracts.research_evidence.UNIT_ID_PATTERN`` の形。
    """
    units = [("title", script.title), ("hook", script.hook)]
    for scene in script.scenes:
        units.append((f"scene:{scene.id}:narration", scene.narration))
        units.append((f"scene:{scene.id}:visual", scene.visual))
    return units


def _kind(triggers: Sequence[str]) -> ClaimKind:
    for trigger, kind in _KIND_BY_TRIGGER:
        if trigger in triggers:
            return kind
    return ClaimKind.EVENT


def claims_from_candidates(candidates: Sequence[CandidateSentence]) -> tuple[ClaimInput, ...]:
    """候補文 → Evidence の claim。題名・フックを ``central`` にして先に取り、上限で切る。"""
    ordered = sorted(
        enumerate(candidates), key=lambda item: (item[1].unit_id not in _CENTRAL_UNITS, item[0])
    )
    seen: set[str] = set()
    claims: list[ClaimInput] = []
    for _, candidate in ordered:
        text = candidate.sentence.strip()[:CLAIM_TEXT_MAX_CHARS]
        key = normalize_claim_text(text)
        if not key or key in seen:
            continue
        seen.add(key)
        claims.append(
            ClaimInput(
                claim_text=text,
                kind=_kind(candidate.triggers),
                importance=ClaimImportance.CENTRAL
                if candidate.unit_id in _CENTRAL_UNITS
                else ClaimImportance.SUPPORTING,
            )
        )
        if len(claims) >= MAX_CLAIMS_PER_REQUEST:
            break
    return tuple(claims)


class ScriptEvidenceActivities:
    """外部依存（DB・ArtifactStore・Gateway・照合・起動・時計）をすべて注入する（INV-18）。"""

    def __init__(
        self,
        *,
        session_factory: async_sessionmaker[AsyncSession],
        store: ArtifactStore,
        gateway: ResearchGateway,
        verifier: ScriptVerifier,
        starter: ResearchWorkflowStarter,
        channel_id: str,
        default_strategy_profile_id: str,
        extractor: ClaimExtractor | None = None,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
        wait_seconds: float = SCRIPT_EVIDENCE_WAIT_SECONDS,
        poll_seconds: float = SCRIPT_EVIDENCE_POLL_SECONDS,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
        heartbeat: Callable[[], None] | None = None,
    ) -> None:
        self._session_factory = session_factory
        self._store = store
        self._gateway = gateway
        self._verifier = verifier
        self._starter = starter
        self._channel_id = channel_id
        self._default_strategy = default_strategy_profile_id
        self._extractor = extractor
        self._clock = clock
        self._wait_seconds = wait_seconds
        self._poll_seconds = poll_seconds
        self._sleep = sleep
        self._heartbeat = heartbeat or activity.heartbeat

    def all_activities(self) -> Sequence[Callable[..., object]]:
        return [self.check]

    @activity.defn(name=SCRIPT_EVIDENCE_CHECK)
    async def check(self, request: ScriptEvidenceRequest) -> ScriptEvidenceOutcome:
        try:
            outcome = await self._check(request)
        except Exception as exc:  # noqa: BLE001 - 助言。型名だけを残す（例外文は写さない / INV-20）
            outcome = ScriptEvidenceOutcome(
                outcome=EvidenceCheckOutcome.ERROR.value, reason=type(exc).__name__
            )
        logger.info(
            "script evidence for episode %s: %s (research %s %s, verdict %s, verification %s)",
            request.episode_id,
            outcome.outcome,
            outcome.research_request_id,
            outcome.research_status,
            outcome.verdict,
            outcome.verification_artifact_id,
        )
        return outcome

    async def _check(self, request: ScriptEvidenceRequest) -> ScriptEvidenceOutcome:
        script = await self._load_script(request)
        units = script_units(script)
        candidates = detect_candidates(units)
        if self._extractor is not None:
            candidates += list(await self._extractor.extract(units))
        claims = claims_from_candidates(candidates)
        if not claims:
            return ScriptEvidenceOutcome(outcome=EvidenceCheckOutcome.NO_CLAIMS.value)

        strategy = await self._strategy(request.episode_id)
        submitted = await self._gateway.submit(
            EvidenceResearchSubmit(
                kind=ResearchKind.EVIDENCE,
                idempotency_key=f"script-evidence:{request.episode_id}:{request.sha256[:32]}",
                requester=REQUESTER,
                channel_id=self._channel_id,
                episode_id=request.episode_id,
                audience=strategy.audience_description[:200],
                language=script.language,
                format_profile="any",
                time_window=EVIDENCE_TIME_WINDOW,
                as_of=self._clock(),
                inputs=EvidenceResearchInputs(claim_inputs=claims),
            )
        )
        research = submitted.request
        if research.status is ResearchStatus.QUEUED:
            await self._starter.start_research(request_id=research.id)
        research = await self._wait(research)
        if research is None:
            return ScriptEvidenceOutcome(
                outcome=EvidenceCheckOutcome.TIMEOUT.value,
                research_request_id=submitted.request.id,
            )
        if research.status is not ResearchStatus.COMPLETED:
            # completed 以外はすべて「調査なし」（INV-37）。照合しない
            code = (research.blocked_reason or {}).get("code")
            return ScriptEvidenceOutcome(
                outcome=EvidenceCheckOutcome.NO_RESEARCH.value,
                research_request_id=research.id,
                research_status=research.status.value,
                reason=None if code is None else str(code),
            )
        return await self._verify(request, script, units, research)

    async def _load_script(self, request: ScriptEvidenceRequest) -> ScriptArtifact:
        payload = await self._store.get_json(request.object_key)
        if sha256_hex(canonical_json_bytes(payload)) != request.sha256:
            raise ValueError("the script artifact sha256 does not match")
        return parse_script_artifact(payload)

    async def _strategy(self, episode_id: str) -> StrategyProfile:
        """Episode の plan の strategy（読むだけ）。plan が無ければ設定の既定。"""
        profile_id = self._default_strategy
        async with self._session_factory() as session:
            episode = await EpisodeRepository(session).get(episode_id)
            if episode is not None and episode.topic_plan_id is not None:
                plan = await TopicPlanRepository(session).get(episode.topic_plan_id)
                if plan is not None:
                    profile_id = plan.strategy_profile_id
        return STRATEGY_PROFILES.get(profile_id) or STRATEGY_PROFILES[self._default_strategy]

    async def _wait(self, research: ResearchRequest) -> ResearchRequest | None:
        """終端（または ``blocked``）まで DB を読む。上限を越えたら ``None``。"""
        loop = asyncio.get_running_loop()
        deadline = loop.time() + self._wait_seconds
        current: ResearchRequest | None = research
        while True:
            if current is None:
                raise LookupError("the research request disappeared")
            if current.status in RESEARCH_TERMINAL_STATUSES or current.status is (
                ResearchStatus.BLOCKED
            ):
                return current
            if loop.time() >= deadline:
                return None
            self._heartbeat()
            await self._sleep(self._poll_seconds)
            current = await self._gateway.get(research.id)

    async def _verify(
        self,
        request: ScriptEvidenceRequest,
        script: ScriptArtifact,
        units: list[tuple[str, str]],
        research: ResearchRequest,
    ) -> ScriptEvidenceOutcome:
        try:
            result = ResearchResult.model_validate(research.result_summary or {})
        except ValidationError:
            result = None
        refs = [
            ref
            for ref in (result.artifact_refs if result is not None else ())
            if ref.artifact_type is ResearchArtifactType.RESEARCH_EVIDENCE
        ]
        if len(refs) != 1:
            return ScriptEvidenceOutcome(
                outcome=EvidenceCheckOutcome.NO_RESEARCH.value,
                research_request_id=research.id,
                research_status=research.status.value,
                reason="evidence_artifact_missing",
            )
        verified = await self._verifier.verify(
            ScriptVerificationRequest(
                evidence_request_id=research.id,
                evidence_artifact=ArtifactDigestRef(
                    artifact_id=refs[0].artifact_id, sha256=refs[0].sha256
                ),
                language=script.language,
                units=tuple(ScriptUnitInput(unit_id=u, text=t) for u, t in units),
                script_ref=ArtifactDigestRef(
                    artifact_id=request.artifact_id, sha256=request.sha256
                ),
                episode_id=request.episode_id,
            )
        )
        return ScriptEvidenceOutcome(
            outcome=EvidenceCheckOutcome.VERIFIED.value,
            research_request_id=research.id,
            research_status=research.status.value,
            verdict=verified.verdict.value,
            verification_artifact_id=verified.artifact_ref.artifact_id,
            verification_sha256=verified.artifact_ref.sha256,
        )
