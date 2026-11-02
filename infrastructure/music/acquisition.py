"""BGM 自動収集の本体 ``AcquisitionService``（ADR-0087 §3、INV-152）。

1 回の ``run`` = 1 つの需要（要件の hash）に対する上限つきの収集::

    レート制限 → 検索語 → 検索（≤4 回）→ imageinfo → decide_license → 事前検査 →
    ダウンロード（≤3 件・1 件 ≤40MB・合計 ≤120MB）→ sha256 重複 → ffprobe/ffmpeg 検査 →
    MinIO へ原本・証拠（読み戻して照合）→ 隔離で登録 → AUTO_APPROVE かつ解析成功のときだけ promote

- ダウンロード直後の曲は**必ず隔離**で登録する。承認 (promote) は規則（``approve_track``）の結果だけ
- 候補ごとの失敗は結果に残して run を続ける。run 全体は例外を外へ出さない（プログラム誤りを除く）
- 同じ sha256 は何も書かない。実行の監査は ``record_acquisition_run`` に 1 回だけ書く
- ネットワークには触れない（``source`` を差し替える。実体は ``infrastructure/music/sources/``）
"""

from __future__ import annotations

import asyncio
import logging
import tempfile
from collections.abc import Callable, Mapping, Sequence
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Protocol

from contracts.music_sources import (
    ACQUISITION_ALLOWED_EXTENSIONS,
    ACQUISITION_MAX_BYTES_PER_FILE,
    ACQUISITION_MAX_BYTES_PER_RUN,
    ACQUISITION_MAX_DOWNLOADS_PER_RUN,
    ACQUISITION_MAX_DURATION_MS,
    ACQUISITION_MAX_SEARCHES_PER_RUN,
    ACQUISITION_MIN_DURATION_MS,
    ACQUISITION_MIN_INTERVAL_SECONDS,
    ACQUISITION_RUN_TIMEOUT_SECONDS,
    AcquisitionCandidateResult,
    AcquisitionOutcome,
    AcquisitionRunSummary,
    LicenseDecisionKind,
    MusicSourceId,
)
from domain.artifact.hashing import canonical_json_bytes, sha256_hex
from domain.errors import ArtifactConflictError
from domain.music.commons_facts import (
    CommonsFileInfo,
    build_evidence_payload,
)
from domain.music.ingest import ORIGINALS_PREFIX
from domain.music.license_policy import approve_track, build_quarantined_track, decide_license
from domain.music.ports import BgmCatalogRepository
from domain.music.search_terms import build_search_queries
from infrastructure.music.audio_inspect import AudioInvalidError, AudioReport
from infrastructure.music.commons_evidence import parse_evidence
from infrastructure.music.sources.commons import COMMONS_API_URL, extension_of
from infrastructure.music.sources.errors import CandidateInvalidError, SourceUnavailableError
from infrastructure.music.sources.types import DownloadedFile
from infrastructure.storage.artifact_store import ArtifactStore

logger = logging.getLogger(__name__)

EVIDENCE_PREFIX = "music/evidence/"
SEARCH_RESULT_LIMIT = 10


class CommonsSource(Protocol):
    async def search_titles(self, query: str, *, limit: int = 10) -> list[str]: ...

    async def imageinfo(self, titles: Sequence[str]) -> list[dict[str, Any]]: ...

    async def download(self, url: str, *, expected_sha1: str) -> DownloadedFile: ...


class AudioInspector(Protocol):
    def inspect(self, path: Path) -> AudioReport: ...


def _result(
    info: CommonsFileInfo,
    outcome: AcquisitionOutcome,
    *,
    decision: LicenseDecisionKind | None = None,
    reasons: tuple[Any, ...] = (),
    sha: str | None = None,
    bytes_downloaded: int = 0,
    track_id: str | None = None,
) -> AcquisitionCandidateResult:
    return AcquisitionCandidateResult(
        source=MusicSourceId.WIKIMEDIA_COMMONS,
        source_page_url=info.facts.source_page_url,
        outcome=outcome,
        license_decision=decision,
        license_reasons=tuple(reasons),
        file_sha256=sha,
        bytes_downloaded=bytes_downloaded,
        track_id=track_id,
    )


class AcquisitionService:
    def __init__(
        self,
        *,
        source: CommonsSource,
        repo: BgmCatalogRepository,
        store: ArtifactStore,
        inspector: AudioInspector,
        clock: Callable[[], datetime],
        allowed_extensions: Sequence[str] | None = None,
    ) -> None:
        self._source = source
        self._repo = repo
        self._store = store
        self._inspector = inspector
        self._clock = clock
        self._allowed = frozenset(allowed_extensions or ACQUISITION_ALLOWED_EXTENSIONS)

    async def run(
        self, demand_sha256: str, requirements: Mapping[str, Any]
    ) -> AcquisitionRunSummary | None:
        """収集を 1 回。``None`` = 同じ需要を最小間隔内に実行済み（何も書かない）。"""
        started = self._clock()
        last = await self._repo.last_acquisition_at(demand_sha256)
        if last is not None and started - last < timedelta(
            seconds=ACQUISITION_MIN_INTERVAL_SECONDS
        ):
            logger.info("bgm acquisition skipped: rate limited")
            return None
        deadline = started + timedelta(seconds=ACQUISITION_RUN_TIMEOUT_SECONDS)
        candidates: list[AcquisitionCandidateResult] = []
        searches = 0

        titles: list[str] = []
        for query in build_search_queries(requirements)[:ACQUISITION_MAX_SEARCHES_PER_RUN]:
            if self._clock() >= deadline:
                break
            searches += 1
            try:
                found = await self._source.search_titles(query, limit=SEARCH_RESULT_LIMIT)
            except SourceUnavailableError as exc:
                logger.warning("bgm acquisition: search unavailable: %s", exc)
                candidates.append(self._unavailable())
                break
            titles.extend(t for t in found if t not in titles)

        pages: list[dict[str, Any]] = []
        if titles and not candidates:
            try:
                pages = await self._source.imageinfo(titles)
            except SourceUnavailableError as exc:
                logger.warning("bgm acquisition: imageinfo unavailable: %s", exc)
                candidates.append(self._unavailable())

        downloads = 0
        total_bytes = 0
        for page in pages:
            if self._clock() >= deadline or downloads >= ACQUISITION_MAX_DOWNLOADS_PER_RUN:
                info = parse_evidence(build_evidence_payload(page, fetched_at="-"))
                candidates.append(_result(info, AcquisitionOutcome.LIMIT_REACHED))
                break
            result, spent = await self._candidate(page, total_bytes)
            candidates.append(result)
            if spent is not None:
                downloads += 1
                total_bytes += spent

        summary = AcquisitionRunSummary(
            demand_sha256=demand_sha256,
            started_at=started,
            finished_at=self._clock(),
            searches=searches,
            candidates=tuple(candidates),
        )
        await self._repo.record_acquisition_run(summary)
        return summary

    @staticmethod
    def _unavailable() -> AcquisitionCandidateResult:
        return AcquisitionCandidateResult(
            source=MusicSourceId.WIKIMEDIA_COMMONS,
            source_page_url=COMMONS_API_URL,
            outcome=AcquisitionOutcome.SOURCE_UNAVAILABLE,
        )

    def _prefilter(self, info: CommonsFileInfo, total_bytes: int) -> AcquisitionOutcome | None:
        url = info.facts.original_file_url or ""
        if extension_of(url) not in self._allowed:
            return AcquisitionOutcome.VALIDATION_FAILED
        if info.mediatype is not None and info.mediatype.upper() != "AUDIO":
            return AcquisitionOutcome.VALIDATION_FAILED
        if info.mime is not None and not info.mime.startswith(("audio/", "application/ogg")):
            return AcquisitionOutcome.VALIDATION_FAILED
        if info.sha1 is None:
            return AcquisitionOutcome.VALIDATION_FAILED
        if info.size_bytes is not None:
            if info.size_bytes > ACQUISITION_MAX_BYTES_PER_FILE:
                return AcquisitionOutcome.VALIDATION_FAILED
            if total_bytes + info.size_bytes > ACQUISITION_MAX_BYTES_PER_RUN:
                return AcquisitionOutcome.LIMIT_REACHED
        if info.duration_ms is not None and not (
            ACQUISITION_MIN_DURATION_MS <= info.duration_ms <= ACQUISITION_MAX_DURATION_MS
        ):
            return AcquisitionOutcome.VALIDATION_FAILED
        return None

    async def _candidate(
        self, page: Mapping[str, Any], total_bytes: int
    ) -> tuple[AcquisitionCandidateResult, int | None]:
        """(結果, 消費したバイト数)。消費 ``None`` = ダウンロードしていない。"""
        now = self._clock()
        payload = build_evidence_payload(
            page, fetched_at=now.isoformat(), api_endpoint=COMMONS_API_URL
        )
        info = parse_evidence(payload)
        facts = info.facts
        decision = decide_license(facts)
        if decision.kind is LicenseDecisionKind.REJECT:
            return _result(
                info,
                AcquisitionOutcome.LICENSE_REJECTED,
                decision=decision.kind,
                reasons=decision.reasons,
            ), None
        skip = self._prefilter(info, total_bytes)
        if skip is not None:
            return _result(info, skip, decision=decision.kind, reasons=decision.reasons), None
        assert facts.original_file_url is not None and info.sha1 is not None  # _prefilter 済み

        try:
            downloaded = await self._source.download(
                facts.original_file_url, expected_sha1=info.sha1
            )
        except (SourceUnavailableError, CandidateInvalidError) as exc:
            logger.warning("bgm acquisition: download failed: %s", exc)
            return _result(
                info,
                AcquisitionOutcome.DOWNLOAD_FAILED,
                decision=decision.kind,
                reasons=decision.reasons,
            ), 0
        spent = len(downloaded.data)
        sha = downloaded.sha256
        common: dict[str, Any] = {
            "decision": decision.kind,
            "reasons": decision.reasons,
            "sha": sha,
            "bytes_downloaded": spent,
        }

        if await self._repo.find_by_sha256(sha) is not None:
            return _result(info, AcquisitionOutcome.DUPLICATE, **common), spent

        try:
            report = await asyncio.to_thread(self._inspect, downloaded)
        except AudioInvalidError as exc:
            logger.warning("bgm acquisition: audio invalid: %s", exc.reason)
            return _result(info, AcquisitionOutcome.VALIDATION_FAILED, **common), spent

        try:
            evidence_key = await self._store_evidence(payload, facts.evidence_sha256)
            object_key = await self._store_original(downloaded, info.mime)
        except (ArtifactConflictError, OSError, ValueError) as exc:
            logger.warning("bgm acquisition: store failed: %s", type(exc).__name__)
            return _result(info, AcquisitionOutcome.VALIDATION_FAILED, **common), spent

        track = build_quarantined_track(
            facts,
            file_sha256=sha,
            object_key=object_key,
            duration_ms=report.duration_ms,
            loudness_lufs=report.integrated_lufs,
            bpm=report.tempo_bpm,
        )
        entry, created = await self._repo.register_quarantined(
            track,
            acquired_from=MusicSourceId.WIKIMEDIA_COMMONS.value,
            acquired_at=now,
            evidence_object_key=evidence_key,
            analysis=report.as_analysis(),
            inferred_tags={},
        )
        if not created:
            return _result(
                info, AcquisitionOutcome.DUPLICATE, track_id=entry.track.track_id, **common
            ), spent

        if decision.kind is LicenseDecisionKind.AUTO_APPROVE:
            await self._repo.promote(
                approve_track(track, facts, decision, checked_at=now), verified_at=now
            )
            outcome = AcquisitionOutcome.REGISTERED_APPROVED
        else:
            outcome = AcquisitionOutcome.REGISTERED_QUARANTINED
        return _result(info, outcome, track_id=track.track_id, **common), spent

    def _inspect(self, downloaded: DownloadedFile) -> AudioReport:
        with tempfile.TemporaryDirectory(prefix="bgm-acquire-") as tmp:
            path = Path(tmp) / f"candidate.{downloaded.extension}"
            path.write_bytes(downloaded.data)
            return self._inspector.inspect(path)

    async def _store_evidence(self, payload: Mapping[str, Any], evidence_sha: str) -> str:
        key = f"{EVIDENCE_PREFIX}{evidence_sha}.json"
        await self._store.put_json(key, payload)
        stored = await self._store.get_json(key)
        if sha256_hex(canonical_json_bytes(stored)) != evidence_sha:
            raise ValueError("evidence readback mismatch")
        return key

    async def _store_original(self, downloaded: DownloadedFile, mime: str | None) -> str:
        key = f"{ORIGINALS_PREFIX}{downloaded.sha256}.{downloaded.extension}"
        await self._store.put_bytes(key, downloaded.data, mime or "application/octet-stream")
        if await self._store.sha256_of(key) != downloaded.sha256:
            raise ValueError("original readback mismatch")
        return key


__all__ = ["AcquisitionService", "AudioInspector", "CommonsSource"]
