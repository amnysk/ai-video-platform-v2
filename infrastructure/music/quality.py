"""BGM の検査セットの producer（ADR-0086 §4）。``bgm_quality_checks`` を包むだけ。

gate は render の使用宣言が used のときだけ呼ぶ。だから「証跡が無い」は宣言との食い違いで、
常に例外にする
（例外 = 記録されない = 必須欠落で REVIEW。PASS に倒れない）。
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable, Sequence
from datetime import datetime
from pathlib import Path

from contracts.music import BgmTrack
from contracts.music_render import BGM_VIDEO_FORMAT_BY_PROFILE_ID
from contracts.quality_records import CheckKey
from domain.music.quality_checks import bgm_quality_checks
from domain.quality.records import CheckSetDraft, ProducerContext
from infrastructure.music.catalog_store import CATALOG_FILE_NAME, CatalogStoreError, read_catalog
from infrastructure.music.evidence import BgmEvidenceError, load_bgm_evidence
from infrastructure.storage.artifact_store import ArtifactStore

PRODUCER = "bgm-quality"
PRODUCER_VERSION = "bgm-quality-1"

CatalogLoader = Callable[[], Sequence[BgmTrack]]


def catalog_loader_for(root: Path) -> CatalogLoader:
    """今の承認済みカタログ。無い/壊れていれば例外（空の表で検査しない）。"""

    def load() -> Sequence[BgmTrack]:
        if not (root / CATALOG_FILE_NAME).is_file():
            raise CatalogStoreError("bgm catalog is missing")
        return read_catalog(root)

    return load


class BgmCheckProducer:
    key = CheckKey.BGM

    def __init__(self, store: ArtifactStore, catalog: CatalogLoader) -> None:
        self._store = store
        self._catalog = catalog

    async def produce(self, context: ProducerContext, *, now: datetime) -> CheckSetDraft | None:
        final = await context.final_video()
        subject = context.subject
        loaded = await load_bgm_evidence(
            self._store,
            episode_id=subject.episode_id,
            media_sha256=final.media.sha256,
            input_hash=subject.input_hash,
        )
        if loaded is None:
            raise BgmEvidenceError("bgm was declared used but its evidence is missing")
        evidence, digest = loaded
        catalog = await asyncio.to_thread(self._catalog)
        checks = bgm_quality_checks(
            final_video_sha256=final.media.sha256,
            evidence=evidence,
            evidence_sha256=digest,
            catalog=catalog,
            now=now,
            intelligibility=evidence.speech,
            video_format=BGM_VIDEO_FORMAT_BY_PROFILE_ID.get(final.render_profile.profile_id),
        )
        return CheckSetDraft(
            key=self.key,
            checks=checks,
            producer=PRODUCER,
            producer_version=PRODUCER_VERSION,
            evidence_ref=digest,
        )
