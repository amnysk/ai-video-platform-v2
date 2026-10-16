"""B6（Research への opt-in 接続）のテスト部品（ADR-0038 §B6 / ADR-0039 §B6。テスト専用）。

- Topic Planner: 固定の ``PlanningContext`` と、契約を通る候補の出力。OFF の prompt の
  golden（``OFF_PROMPT_SHA256``）は **f209e7c の** ``workers/planning/topic_activities.py`` で同じ入
  力から取った値（``git archive f209e7c`` を展開した木で ``generate_candidates`` を呼んで sha256 を
  取った）
- 台本: Evidence の Fake コーパス（``infrastructure/research/fake_corpus.py``）に当たる台本と、
  ``ResearchWorkflow`` の代わりに実行器をその場で走らせる起動器
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from typing import Any

from contracts.artifacts import build_script_artifact
from contracts.topic_planning import (
    DEFAULT_PLANNER_POLICY,
    AnalyticsSummary,
    GenerateCandidatesRequest,
    MemoryItem,
    PlanningContext,
    TopicPlannerInput,
)
from domain.artifact.hashing import canonical_json_bytes, sha256_hex
from infrastructure.research.executor import ResearchExecutor
from infrastructure.storage.artifact_store import ArtifactStore

#: f209e7c の ``generate_candidates`` が ``planner_request()`` から作った prompt の sha256 と版
OFF_PROMPT_SHA256 = "4838783892e30e6450c69a9387a2a797e82274efd3f6274aadb59dcae503ad0c"
OFF_PROMPT_VERSION = "topic_en@2"
PLAN_DATE = "2026-09-30"


def _candidate(subject: str) -> dict[str, Any]:
    return {
        "topic": f"The untold story of {subject}",
        "subject": subject,
        "entities": [],
        "era": "edo",
        "theme": "daily_life",
        "angle": "reason",
        "hook": f"Hook about {subject}",
        "visual_concept": f"Visual of {subject}",
        "reason": "Fits the audience",
        "audience_fit": 0.8,
        "visual_fit": 0.7,
    }


#: 契約（最小件数）を通る候補の出力
CANDIDATES_OUTPUT = json.dumps(
    {
        "candidates": [
            _candidate(f"subject_{i}") for i in range(DEFAULT_PLANNER_POLICY.candidate_count_min)
        ]
    }
)


def planner_request() -> GenerateCandidatesRequest:
    context = PlanningContext(
        request=TopicPlannerInput(plan_date=PLAN_DATE),
        analytics=AnalyticsSummary(mode="no_analytics"),
        memory=[
            MemoryItem(
                topic="Why sumo wrestlers throw salt",
                subject="sumo_salt",
                entities=[],
                era="edo",
                theme="ritual_and_religion",
                angle="reason",
                day="2026-09-01",
                status="uploaded",
            )
        ],
    )
    return GenerateCandidatesRequest(context=context, round=2, avoid_subjects=["ninja_myths"])


# ------------------------------------------------------------------ 台本と Evidence

EPISODE_ID = "5b4f2f0e-8f7a-4c1e-9a55-0d1f0a3b6c11"
SCRIPT_ARTIFACT_ID = "0c9d3a57-2f45-4f1b-8f3e-6a1c2b3d4e5f"
#: Fake コーパスの資料が支える主張（鉄砲伝来 1543 年・異説 1542 年）
EVIDENCE_NARRATION = "鉄砲伝来は1543年とされるが、1542年とする異説もある。"


def script_payload(*narrations: str, language: str = "ja") -> dict[str, Any]:
    lines = list(narrations) or [EVIDENCE_NARRATION]
    while len(lines) < 3:
        lines.append("人々は新しい道具に驚いた")
    return build_script_artifact(
        episode_id=EPISODE_ID,
        language=language,
        title="鉄砲の話",
        hook="島に鉄砲が来た",
        scenes=[
            {
                "id": f"s{i + 1}",
                "narration": text,
                "visual": "浜辺の船",
                "duration_ms": 5_000,
            }
            for i, text in enumerate(lines)
        ],
        metadata={"topic": "鉄砲伝来", "generator": "codex", "generator_model": "fake"},
    )


async def store_script(store: ArtifactStore, payload: dict[str, Any]) -> tuple[str, str]:
    """(object key, sha256)。本番の台本と同じ正準 JSON で保存する。"""
    sha = sha256_hex(canonical_json_bytes(payload))
    key = f"episodes/{EPISODE_ID}/script/{sha}.json"
    await store.put_json(key, payload)
    return key, sha


class InlineResearchStarter:
    """``ResearchWorkflow`` の代わりに実行器をその場で走らせる（``run=False`` なら起動しない）。"""

    def __init__(self, executor: ResearchExecutor | None, *, run: bool = True) -> None:
        self._executor = executor
        self._run = run
        self.started: list[str] = []

    async def start_research(self, *, request_id: str) -> str:
        self.started.append(request_id)
        if self._run and self._executor is not None:
            await self._executor.execute(request_id)
        return f"research-{request_id}"


FIXED_NOW = datetime(2026, 9, 30, 0, 0, tzinfo=UTC)
