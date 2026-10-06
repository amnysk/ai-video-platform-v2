"""台本の照合（ADR-0038 §4）: 台本の文面 + Evidence の成果物 → ``research_script_verification``。

外部を呼ばない（評価器も検索も使わない。台帳に行を作らない）。照合の規則は
``domain/research/script_verification.py``（純粋）にあり、ここは読み込み・検証・保存だけを持つ:

1. Evidence の依頼を読み、指定された成果物がその依頼の**現行の** ``research_evidence`` 行であり、
   キーが research のキーで、ArtifactStore から読み戻した sha256 が記録と一致することを確かめる
   （Gateway の再利用前の検証と同じ考え方。外れたら照合しない）
2. 照合する（Evidence の依頼が ``completed`` でなければ結論は ``insufficient``。合格にしない）
3. 結果を正準 JSON で ``research/{evidence_request_id}/research_script_verification/{sha}.json`` に
   書き、読み戻して照合してから ``research_artifacts`` に記録する。**所有者は Evidence の依頼**
   （``episode_id`` は参照だけ）。依頼の状態は変えない

``ClaimExtractor`` は決定的な網への上乗せで、registry は ``fake`` のときだけ組む（実 LLM の抽出器は
配線しない。配線するなら、その呼び出しを台帳の枠と金額の上限に載せる ADR を先に置く）。
"""

from __future__ import annotations

from dataclasses import dataclass

from pydantic import ValidationError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from contracts.research import (
    ResearchArtifactRef,
    ResearchArtifactType,
    ResearchKind,
)
from contracts.research_evidence import (
    SCRIPT_VERIFICATION_SCHEMA_VERSION,
    ScriptVerificationRequest,
    VerificationVerdict,
    parse_evidence_artifact,
)
from domain.artifact.hashing import canonical_json_bytes, sha256_hex
from domain.research.errors import ResearchArtifactReadbackError, ResearchInputInvalidError
from domain.research.evidence_ports import ClaimExtractor
from domain.research.keys import research_artifact_object_key
from domain.research.script_verification import verify_script
from infrastructure.db.research_repositories import (
    ResearchArtifactRepository,
    ResearchRequestRepository,
)
from infrastructure.storage.artifact_store import ArtifactStore, readback_sha256

__all__ = ["ScriptVerificationOutcome", "ScriptVerifier"]


@dataclass(frozen=True, slots=True)
class ScriptVerificationOutcome:
    verdict: VerificationVerdict
    artifact_ref: ResearchArtifactRef


class ScriptVerifier:
    def __init__(
        self,
        *,
        session_factory: async_sessionmaker[AsyncSession],
        store: ArtifactStore,
        bucket: str,
        extractor: ClaimExtractor | None = None,
    ) -> None:
        self._session_factory = session_factory
        self._store = store
        self._bucket = bucket
        self._extractor = extractor

    async def verify(self, request: ScriptVerificationRequest) -> ScriptVerificationOutcome:
        request_id = request.evidence_request_id
        ref = request.evidence_artifact
        async with self._session_factory() as session:
            evidence_request = await ResearchRequestRepository(session).get(request_id)
            current = await ResearchArtifactRepository(session).list_current(request_id)
        if evidence_request is None or evidence_request.kind is not ResearchKind.EVIDENCE:
            raise ResearchInputInvalidError(f"no evidence research request {request_id}")
        record = next(
            (
                a
                for a in current
                if a.id == ref.artifact_id
                and a.sha256 == ref.sha256
                and a.artifact_type is ResearchArtifactType.RESEARCH_EVIDENCE
            ),
            None,
        )
        if record is None:
            raise ResearchInputInvalidError(
                "the evidence artifact is not the current research_evidence of that request"
            )
        expected = research_artifact_object_key(request_id, record.artifact_type, record.sha256)
        if record.object_key != expected:
            raise ResearchInputInvalidError("the evidence artifact key is not a research key")
        try:
            actual = await readback_sha256(self._store, record.object_key)
        except KeyError as exc:
            raise ResearchArtifactReadbackError("the evidence artifact body is missing") from exc
        if actual != record.sha256:
            raise ResearchArtifactReadbackError("the evidence artifact sha256 does not match")
        try:
            evidence = parse_evidence_artifact(await self._store.get_json(record.object_key))
        except ValidationError as exc:
            raise ResearchInputInvalidError("the evidence artifact violates its contract") from exc

        extra = (
            list(await self._extractor.extract([(u.unit_id, u.text) for u in request.units]))
            if self._extractor is not None
            else []
        )
        payload = verify_script(
            request, evidence, evidence_status=evidence_request.status, extra_candidates=extra
        )
        sha = sha256_hex(canonical_json_bytes(payload))
        artifact_type = ResearchArtifactType.RESEARCH_SCRIPT_VERIFICATION
        key = research_artifact_object_key(request_id, artifact_type, sha)
        put = await self._store.put_json(key, payload)
        if put.sha256 != sha or await readback_sha256(self._store, key) != sha:
            raise ResearchArtifactReadbackError("script verification readback mismatch")
        async with self._session_factory() as session:
            recorded = await ResearchArtifactRepository(session).record(
                request_id=request_id,
                artifact_type=artifact_type,
                schema_version=SCRIPT_VERIFICATION_SCHEMA_VERSION,
                bucket=self._bucket,
                object_key=key,
                sha256=sha,
                size_bytes=put.size,
            )
            await session.commit()
        return ScriptVerificationOutcome(
            verdict=VerificationVerdict(payload["verdict"]),
            artifact_ref=ResearchArtifactRef(
                artifact_type=artifact_type, artifact_id=recorded.id, sha256=sha
            ),
        )
