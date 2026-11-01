"""品質ゲートのサービスを組む唯一の場所（ADR-0081 §7）。worker と API が同じ関数を使う。

``AVP_QUALITY_GATE_ENFORCE`` が OFF のときは何も組み立てない（``None``。MinIO クライアントも
作らない / INV-130）。
BGM・地図の producer は ``build_quality_producers`` に登録する（ADR-0086 §4）。
"""

from __future__ import annotations

from pathlib import Path

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from domain.quality.records import CheckSetProducer, QualityRecordWriter
from infrastructure.config import Settings
from infrastructure.db.quality_repositories import SqlQualityRecordWriter
from infrastructure.geo.quality import MapCheckProducer
from infrastructure.music.credits import BgmCreditSource
from infrastructure.music.quality import BgmCheckProducer, catalog_loader_for
from infrastructure.quality.service import QualityGateService
from infrastructure.storage.artifact_store import ArtifactStore
from infrastructure.storage.minio_store import MinioArtifactStore


def build_quality_producers(
    settings: Settings, store: ArtifactStore
) -> tuple[CheckSetProducer, ...]:
    """gate 時点に呼ぶ producer の列（技術 QA・説明欄は ``QualityGateService`` が持つ）。

    BGM と地図は、render の使用宣言が used のときだけ gate が呼ぶ。だからここは flag を読まない
    （``BGM_ENABLED`` / ``GEO_MAPS_ENABLED`` が OFF なら宣言は not used で、producer は呼ばれない。
    ADR-0086 §4）。
    """
    return (
        BgmCheckProducer(store, catalog_loader_for(Path(settings.ai_video_work_root) / "bgm")),
        MapCheckProducer(store),
    )


def build_quality_record_writer(
    settings: Settings, session_factory: async_sessionmaker[AsyncSession]
) -> QualityRecordWriter | None:
    """render の使用宣言の書き込み口（ADR-0086 §2）。ENFORCE が OFF なら ``None``。"""
    if not settings.quality_gate_enforce:
        return None
    return SqlQualityRecordWriter(session_factory)


def build_quality_service(
    settings: Settings, session_factory: async_sessionmaker[AsyncSession]
) -> QualityGateService | None:
    if not settings.quality_gate_enforce:
        return None
    store = MinioArtifactStore.from_settings(settings)
    return QualityGateService(
        session_factory=session_factory,
        store=store,
        producers=build_quality_producers(settings, store),
        destination_id=settings.youtube_channel_id,
        credit_source=BgmCreditSource(store, session_factory),
    )
