"""Evidence Research のテスト部品（ADR-0038。テスト専用）。

依頼の作成・実行器の組み立て・保存済み成果物の読み出しを 1 か所にまとめる。Fake Provider と
固定コーパス（``infrastructure/research/fake_corpus.py``）だけを使い、実ネットワークに出ない。
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from contracts.research import ResearchArtifactType, parse_research_spec
from contracts.research_evidence import EvidenceArtifact, parse_evidence_artifact
from domain.research.evidence_handler import EvidenceHandler
from domain.research.evidence_ports import EvidenceAssessor
from infrastructure.db.research_repositories import (
    ResearchArtifactRepository,
    ResearchRequestRepository,
)
from infrastructure.research.executor import ResearchExecutor
from infrastructure.research.fake_evidence import FakeEvidenceAssessor
from infrastructure.research.fake_providers import FakeContentFetcher, FakeSearchProvider
from infrastructure.research.registry import CostModel, ResearchProviders
from tests.support.research import evidence_payload

PCV = "provider-config-1+fake"

#: 鉄砲伝来（1543 年）。異説（1542 年）を述べる資料がある。コーパスの timeout 資料に当たらない
TANEGASHIMA_CLAIM = {
    "claim_text": "鉄砲伝来は1543年とされる",
    "kind": "year",
    "importance": "central",
}
#: 明治維新（1868 年）。年代のずれた資料・403 の資料がある
MEIJI_CLAIM = {
    "claim_text": "明治維新は1868年に始まった",
    "kind": "year",
    "importance": "supporting",
}


def claims_payload(*claims: dict[str, Any], **overrides: Any) -> dict[str, Any]:
    payload = evidence_payload(**overrides)
    payload["language"] = "ja"
    payload["inputs"] = {"claim_inputs": list(claims or (TANEGASHIMA_CLAIM, MEIJI_CLAIM))}
    return payload


async def make_request(session_factory, payload: dict[str, Any], key: str = "evidence:b4") -> str:
    async with session_factory() as session:
        created = await ResearchRequestRepository(session).create_or_get(
            idempotency_key=key, spec=parse_research_spec(payload), provider_config_version=PCV
        )
        await session.commit()
    return created.id


def evidence_providers(
    *,
    search: FakeSearchProvider | None = None,
    fetcher: FakeContentFetcher | None = None,
    assessor: EvidenceAssessor | None = None,
    with_assessor: bool = True,
) -> ResearchProviders:
    return ResearchProviders(
        mode="fake",
        search=search or FakeSearchProvider(),
        fetcher=fetcher or FakeContentFetcher(),
        is_real=False,
        assessor=(assessor or FakeEvidenceAssessor()) if with_assessor else None,
    )


def evidence_executor(
    session_factory, store, providers: ResearchProviders | None = None
) -> ResearchExecutor:
    handler = EvidenceHandler()
    return ResearchExecutor(
        session_factory=session_factory,
        store=store,
        bucket="artifacts",
        providers=providers or evidence_providers(),
        handlers={handler.kind: handler},
        cost_model=CostModel(),
        clock=lambda: datetime.now(UTC),
    )


async def stored_evidence(session_factory, store, request_id: str) -> tuple[Any, EvidenceArtifact]:
    """(現行の成果物の行, 本体)。"""
    async with session_factory() as session:
        record = await ResearchArtifactRepository(session).find_current(
            request_id, ResearchArtifactType.RESEARCH_EVIDENCE
        )
    assert record is not None
    return record, parse_evidence_artifact(await store.get_json(record.object_key))
