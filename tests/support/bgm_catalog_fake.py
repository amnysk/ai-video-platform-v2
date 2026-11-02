"""``BgmCatalogRepository`` のインメモリ実装（収集・選曲・取込のテストで共用）。

PostgreSQL 版と**同じ契約**を守る: 同じ sha256 は何も書かず既存を返す、隔離でない曲は
``ValueError``、``save_selection_once`` は後勝ちしない。呼び出しの記録（``calls``）で
「書かなかった」ことも検査できる。
"""

from __future__ import annotations

from collections.abc import Mapping
from datetime import datetime
from typing import Any

from contracts.music import BgmTrack
from contracts.music_selection import BgmSelectionDecision
from contracts.music_sources import AcquisitionRunSummary
from domain.music.ports import CatalogEntry


class InMemoryBgmCatalog:
    def __init__(self) -> None:
        self.entries: dict[str, CatalogEntry] = {}
        self.runs: list[AcquisitionRunSummary] = []
        self.run_times: dict[str, datetime] = {}
        self.demands: dict[str, tuple[Mapping[str, Any], datetime]] = {}
        self.selections: dict[str, BgmSelectionDecision] = {}
        self.calls: list[str] = []

    def _by_sha(self, sha: str) -> CatalogEntry | None:
        return next((e for e in self.entries.values() if e.track.file_sha256 == sha), None)

    async def list_entries(self) -> tuple[CatalogEntry, ...]:
        return tuple(self.entries[k] for k in sorted(self.entries))

    async def find_by_sha256(self, file_sha256: str) -> CatalogEntry | None:
        return self._by_sha(file_sha256)

    async def recent_usage(self, *, window: int) -> dict[str, int]:
        usage: dict[str, int] = {}
        for decision in list(self.selections.values())[-window:]:
            if decision.track_id:
                usage[decision.track_id] = usage.get(decision.track_id, 0) + 1
        return usage

    async def register_quarantined(
        self,
        track: BgmTrack,
        *,
        acquired_from: str,
        acquired_at: datetime,
        evidence_object_key: str | None,
        analysis: Mapping[str, Any],
        inferred_tags: Mapping[str, Any],
    ) -> tuple[CatalogEntry, bool]:
        if track.status != "quarantined" or track.license_status != "unverified":
            raise ValueError("register_quarantined needs a quarantined, unverified track")
        existing = self._by_sha(track.file_sha256)
        if existing is not None:
            return existing, False
        self.calls.append("register_quarantined")
        entry = CatalogEntry(
            track=track,
            acquired_from=acquired_from,
            acquired_at=acquired_at,
            evidence_object_key=evidence_object_key,
            analysis=dict(analysis),
            inferred_tags=dict(inferred_tags),
            last_verified_at=None,
        )
        self.entries[track.track_id] = entry
        return entry, True

    async def promote(self, track: BgmTrack, *, verified_at: datetime) -> CatalogEntry:
        current = self.entries.get(track.track_id)
        if current is None or current.track.status != "quarantined":
            raise ValueError("promote needs a quarantined track")
        if track.license_status != "approved" or track.status != "active":
            raise ValueError("promote needs an approved, active track")
        self.calls.append("promote")
        entry = CatalogEntry(
            track=track,
            acquired_from=current.acquired_from,
            acquired_at=current.acquired_at,
            evidence_object_key=current.evidence_object_key,
            analysis=current.analysis,
            inferred_tags=current.inferred_tags,
            last_verified_at=verified_at,
        )
        self.entries[track.track_id] = entry
        return entry

    async def record_acquisition_run(self, summary: AcquisitionRunSummary) -> None:
        self.calls.append("record_acquisition_run")
        self.runs.append(summary)
        self.run_times[summary.demand_sha256] = summary.finished_at

    async def last_acquisition_at(self, demand_sha256: str) -> datetime | None:
        return self.run_times.get(demand_sha256)

    async def request_acquisition(
        self, demand_sha256: str, requirements_json: Mapping[str, Any], *, requested_at: datetime
    ) -> None:
        self.demands.setdefault(demand_sha256, (dict(requirements_json), requested_at))

    async def pending_demands(self, *, limit: int) -> tuple[tuple[str, Mapping[str, Any]], ...]:
        pending = [
            (sha, req)
            for sha, (req, at) in sorted(self.demands.items(), key=lambda kv: (kv[1][1], kv[0]))
            if sha not in self.run_times
        ]
        return tuple(pending[:limit])

    async def get_selection(self, episode_id: str) -> BgmSelectionDecision | None:
        return self.selections.get(episode_id)

    async def save_selection_once(
        self, decision: BgmSelectionDecision, *, decided_at: datetime
    ) -> BgmSelectionDecision:
        return self.selections.setdefault(decision.episode_id, decision)

    async def finalize_selection(
        self, decision: BgmSelectionDecision, *, finalized_at: datetime
    ) -> BgmSelectionDecision:
        return self.selections.setdefault(decision.episode_id, decision)


__all__ = ["InMemoryBgmCatalog"]
