"""AcquisitionService（INV-152）。偽の source とインメモリ catalog で、通信も DB も使わない。"""

from __future__ import annotations

import copy
import hashlib
import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

from contracts.music_sources import (
    ACQUISITION_MAX_BYTES_PER_FILE,
    ACQUISITION_MAX_DOWNLOADS_PER_RUN,
    ACQUISITION_MAX_SEARCHES_PER_RUN,
    BGM_LICENSE_POLICY_VERSION,
    AcquisitionOutcome,
    AcquisitionRunSummary,
    LicenseReason,
)
from domain.music.commons_facts import pages_from_response
from infrastructure.music.acquisition import AcquisitionService
from infrastructure.music.audio_inspect import AudioInvalidError, AudioReport
from infrastructure.music.sources.errors import CandidateInvalidError, SourceUnavailableError
from infrastructure.music.sources.types import DownloadedFile
from infrastructure.storage.memory_store import InMemoryArtifactStore
from tests.support.bgm_catalog_fake import InMemoryBgmCatalog

FIX = Path(__file__).resolve().parents[1] / "fixtures" / "bgm_commons"
T0 = datetime(2026, 11, 2, 9, 0, tzinfo=UTC)
DEMAND = "d" * 64
REQ = {"mood": "calm", "culture": "japanese", "energy": "low", "tempo_band": "slow"}


def fixture_page(name: str, data: bytes, *, ext: str = "flac", duration_s: float = 120.0) -> dict:
    page = pages_from_response(json.loads((FIX / f"{name}.json").read_text("utf-8")))[0]
    page = copy.deepcopy(page)
    info = page["imageinfo"][0]
    info["url"] = f"https://upload.wikimedia.org/wikipedia/commons/a/ab/{name}.{ext}"
    info["sha1"] = hashlib.sha1(data).hexdigest()  # noqa: S324
    info["size"] = len(data)
    info["duration"] = duration_s
    info["mime"] = "audio/flac"
    info["mediatype"] = "AUDIO"
    page["title"] = f"File:{name}.{ext}"
    return page


class FakeSource:
    def __init__(self, pages: list[dict], blobs: dict[str, bytes]) -> None:
        self.pages = pages
        self.blobs = blobs
        self.searches: list[str] = []
        self.downloads: list[str] = []
        self.search_error: Exception | None = None
        self.download_errors: dict[str, Exception] = {}

    async def search_titles(self, query: str, *, limit: int = 10) -> list[str]:
        self.searches.append(query)
        if self.search_error:
            raise self.search_error
        return [p["title"] for p in self.pages]

    async def imageinfo(self, titles):
        return [p for p in self.pages if p["title"] in titles]

    async def download(self, url: str, *, expected_sha1: str) -> DownloadedFile:
        self.downloads.append(url)
        if url in self.download_errors:
            raise self.download_errors[url]
        data = self.blobs[url]
        return DownloadedFile(
            data,
            hashlib.sha256(data).hexdigest(),
            hashlib.sha1(data).hexdigest(),
            url.rsplit(".", 1)[1],
        )  # noqa: S324


class FakeInspector:
    def __init__(self, fail: str | None = None) -> None:
        self.fail = fail
        self.calls = 0

    def inspect(self, path: Path) -> AudioReport:
        self.calls += 1
        assert path.is_file()
        if self.fail:
            raise AudioInvalidError(self.fail)
        return AudioReport("flac", "flac", 120_000, 44100, 2, -18.0, -1.0)


class Clock:
    def __init__(self) -> None:
        self.now = T0

    def __call__(self) -> datetime:
        return self.now


def build(names: list[str], *, inspector=None):
    blobs: dict[str, bytes] = {}
    pages = []
    for i, n in enumerate(names):
        data = f"audio-{n}-".encode() * (50 + i)
        page = fixture_page(n, data)
        blobs[page["imageinfo"][0]["url"]] = data
        pages.append(page)
    source = FakeSource(pages, blobs)
    repo = InMemoryBgmCatalog()
    store = InMemoryArtifactStore()
    clock = Clock()
    svc = AcquisitionService(
        source=source, repo=repo, store=store, inspector=inspector or FakeInspector(), clock=clock
    )
    return svc, source, repo, store, clock


def outcomes(summary: AcquisitionRunSummary | None) -> list[AcquisitionOutcome]:
    assert summary is not None
    return [c.outcome for c in summary.candidates]


async def test_cc0_own_work_is_registered_then_promoted_with_evidence() -> None:
    svc, source, repo, store, _ = build(["ck61"])
    summary = await svc.run(DEMAND, REQ)
    assert outcomes(summary) == [AcquisitionOutcome.REGISTERED_APPROVED]
    assert repo.calls == ["register_quarantined", "promote", "record_acquisition_run"]
    [entry] = await repo.list_entries()
    t = entry.track
    assert t.status == "active" and t.license_status == "approved"
    assert (
        t.license_approver_kind == "policy"
        and t.license_policy_version == BGM_LICENSE_POLICY_VERSION
    )
    assert (
        t.license_evidence_sha256
        and entry.evidence_object_key == f"music/evidence/{t.license_evidence_sha256}.json"
    )
    assert entry.evidence_object_key
    assert await store.exists(t.object_key) and await store.exists(entry.evidence_object_key)
    assert await store.sha256_of(t.object_key) == t.file_sha256
    assert t.object_key == f"music/originals/{t.file_sha256}.flac"
    assert entry.analysis["integrated_lufs"] == -18.0 and entry.acquired_from == "wikimedia_commons"
    assert summary is not None and summary.searches <= ACQUISITION_MAX_SEARCHES_PER_RUN
    assert repo.runs == [summary]


async def test_review_license_is_registered_quarantined_and_never_promoted() -> None:
    svc, _, repo, _, _ = build(["eastern", "shika"])
    summary = await svc.run(DEMAND, REQ)
    assert outcomes(summary) == [AcquisitionOutcome.REGISTERED_QUARANTINED] * 2
    assert "promote" not in repo.calls
    for e in await repo.list_entries():
        assert e.track.status == "quarantined" and e.track.license_status == "unverified"
    reasons = {r for c in summary.candidates for r in c.license_reasons}
    assert LicenseReason.ATTRIBUTION_LICENSE_NEEDS_REVIEW in reasons
    assert LicenseReason.PUBLIC_DOMAIN_NEEDS_REVIEW in reasons


async def test_rejected_license_downloads_and_writes_nothing() -> None:
    svc, source, repo, store, _ = build(["ck61"])
    info = source.pages[0]["imageinfo"][0]["extmetadata"]
    info["LicenseShortName"]["value"] = "CC BY-NC 4.0"
    info["License"]["value"] = "cc-by-nc-4.0"
    summary = await svc.run(DEMAND, REQ)
    assert outcomes(summary) == [AcquisitionOutcome.LICENSE_REJECTED]
    assert source.downloads == [] and repo.entries == {} and store._objects == {}  # noqa: SLF001
    assert repo.calls == ["record_acquisition_run"]


async def test_analysis_failure_registers_nothing() -> None:
    svc, _, repo, store, _ = build(["ck61"], inspector=FakeInspector(fail="decode_failed"))
    summary = await svc.run(DEMAND, REQ)
    assert outcomes(summary) == [AcquisitionOutcome.VALIDATION_FAILED]
    assert repo.entries == {} and store._objects == {}  # noqa: SLF001


async def test_duplicate_sha256_writes_nothing() -> None:
    svc, source, repo, store, clock = build(["ck61"])
    await svc.run(DEMAND, REQ)
    writes = (list(repo.calls), len(store._objects))  # noqa: SLF001
    clock.now = T0 + timedelta(days=2)
    summary = await svc.run(DEMAND, REQ)
    assert outcomes(summary) == [AcquisitionOutcome.DUPLICATE]
    assert repo.calls == [*writes[0], "record_acquisition_run"]
    assert len(store._objects) == writes[1]  # noqa: SLF001


async def test_rate_limit_per_demand_and_release_after_interval() -> None:
    svc, source, repo, _, clock = build(["ck61"])
    assert await svc.run(DEMAND, REQ) is not None
    n = len(source.searches)
    clock.now = T0 + timedelta(hours=23)
    assert await svc.run(DEMAND, REQ) is None
    assert len(source.searches) == n and len(repo.runs) == 1
    other = await svc.run("e" * 64, REQ)  # 別の需要は別枠
    assert other is not None
    clock.now = T0 + timedelta(hours=25)
    assert await svc.run(DEMAND, REQ) is not None


async def test_download_cap_per_run() -> None:
    svc, source, repo, _, _ = build(["ck61", "kuniholm", "piano051"])
    # 同じ fixture を別名で足して 5 件にする
    for extra in ("x1", "x2"):
        data = f"extra-{extra}".encode() * 80
        page = fixture_page("ck61", data)
        page["title"] = f"File:{extra}.flac"
        page["imageinfo"][0]["url"] = f"https://upload.wikimedia.org/a/{extra}.flac"
        source.pages.append(page)
        source.blobs[page["imageinfo"][0]["url"]] = data
    summary = await svc.run(DEMAND, REQ)
    assert len(source.downloads) == ACQUISITION_MAX_DOWNLOADS_PER_RUN
    assert outcomes(summary)[-1] is AcquisitionOutcome.LIMIT_REACHED


async def test_per_file_and_per_run_byte_caps_are_checked_before_download() -> None:
    svc, source, repo, _, _ = build(["ck61"])
    source.pages[0]["imageinfo"][0]["size"] = ACQUISITION_MAX_BYTES_PER_FILE + 1
    summary = await svc.run(DEMAND, REQ)
    assert outcomes(summary) == [AcquisitionOutcome.VALIDATION_FAILED] and source.downloads == []


async def test_run_byte_budget_stops_further_downloads(monkeypatch: pytest.MonkeyPatch) -> None:
    svc, source, _, _, _ = build(["ck61", "kuniholm", "piano051"])
    first = source.pages[0]["imageinfo"][0]["size"]
    monkeypatch.setattr(
        "infrastructure.music.acquisition.ACQUISITION_MAX_BYTES_PER_RUN", first + 10
    )
    summary = await svc.run(DEMAND, REQ)
    assert len(source.downloads) == 1
    assert outcomes(summary) == [
        AcquisitionOutcome.REGISTERED_APPROVED,
        AcquisitionOutcome.LIMIT_REACHED,
        AcquisitionOutcome.LIMIT_REACHED,
    ]


async def test_duration_and_extension_prefilter_do_not_download() -> None:
    svc, source, _, _, _ = build(["ck61", "kuniholm"])
    source.pages[0]["imageinfo"][0]["duration"] = 5.0
    source.pages[1]["imageinfo"][0]["url"] = "https://upload.wikimedia.org/a/x.exe"
    summary = await svc.run(DEMAND, REQ)
    assert outcomes(summary) == [AcquisitionOutcome.VALIDATION_FAILED] * 2
    assert source.downloads == []


async def test_download_failure_continues_the_run() -> None:
    svc, source, repo, _, _ = build(["ck61", "kuniholm"])
    first = source.pages[0]["imageinfo"][0]["url"]
    source.download_errors[first] = SourceUnavailableError("timeout")
    summary = await svc.run(DEMAND, REQ)
    assert outcomes(summary) == [
        AcquisitionOutcome.DOWNLOAD_FAILED,
        AcquisitionOutcome.REGISTERED_APPROVED,
    ]


async def test_candidate_invalid_download_is_a_failure_not_an_exception() -> None:
    svc, source, _, _, _ = build(["ck61"])
    source.download_errors[source.pages[0]["imageinfo"][0]["url"]] = CandidateInvalidError(
        "sha1_mismatch"
    )
    summary = await svc.run(DEMAND, REQ)
    assert outcomes(summary) == [AcquisitionOutcome.DOWNLOAD_FAILED]


async def test_search_outage_is_recorded_and_does_not_raise() -> None:
    svc, source, repo, _, _ = build(["ck61"])
    source.search_error = SourceUnavailableError("503")
    summary = await svc.run(DEMAND, REQ)
    assert outcomes(summary) == [AcquisitionOutcome.SOURCE_UNAVAILABLE]
    assert len(source.searches) == 1 and repo.entries == {}


async def test_programming_errors_are_not_swallowed() -> None:
    svc, source, _, _, _ = build(["ck61"])

    async def boom(*a: Any, **k: Any):
        raise RuntimeError("bug")

    source.download = boom  # type: ignore[method-assign]
    with pytest.raises(RuntimeError):
        await svc.run(DEMAND, REQ)


async def test_corrupted_storage_readback_is_not_registered() -> None:
    class Corrupt(InMemoryArtifactStore):
        async def sha256_of(self, key: str) -> str:
            return "0" * 64

    svc, source, repo, _, clock = build(["ck61"])
    svc._store = Corrupt()  # noqa: SLF001
    summary = await svc.run(DEMAND, REQ)
    assert outcomes(summary) == [AcquisitionOutcome.VALIDATION_FAILED] and repo.entries == {}


async def test_run_deadline_stops_the_run() -> None:
    svc, source, repo, _, clock = build(["ck61", "kuniholm"])
    orig = source.download

    async def slow(url: str, *, expected_sha1: str):
        clock.now += timedelta(minutes=11)
        return await orig(url, expected_sha1=expected_sha1)

    source.download = slow  # type: ignore[method-assign]
    summary = await svc.run(DEMAND, REQ)
    assert len(source.downloads) == 1
    assert outcomes(summary)[-1] is AcquisitionOutcome.LIMIT_REACHED
