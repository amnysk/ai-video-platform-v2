"""Agent が呼んでよい tool の語彙。**唯一の定義**（AGENTS.md §8 / ADR-0073 / INV-103）。

許可は read-only の6つだけ。課金・投稿・DB 変更・Schedule・予約解放は Agent に渡さない
（既存の制御層とオーナー承認が要る。設計 v3 §7）。tool の実行器・登録はここに無い（語彙だけ）。
"""

from __future__ import annotations

from enum import StrEnum


class PolicyToolId(StrEnum):
    RESEARCH_SEARCH = "research.search"
    RESEARCH_FETCH = "research.fetch"
    GEO_RESOLVE = "geo.resolve"
    GEO_VERIFY = "geo.verify"
    BGM_LIST_APPROVED = "bgm.list_approved"
    ARTIFACT_READ = "artifact.read"


#: 許可してはならない tool id。allowlist と交わらないことをテストで固定する。
FORBIDDEN_POLICY_TOOLS: frozenset[str] = frozenset(
    {
        "fal.generate",
        "youtube.upload",
        "db.migrate",
        "schedule.pause",
        "provider_reservations.release",
    }
)

ALLOWED_POLICY_TOOLS: frozenset[str] = frozenset(t.value for t in PolicyToolId)


class PolicyRejection(StrEnum):
    FORBIDDEN = "forbidden"
    NOT_ALLOWLISTED = "not_allowlisted"
    MALFORMED = "malformed"
