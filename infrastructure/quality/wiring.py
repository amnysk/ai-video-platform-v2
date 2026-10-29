"""品質ゲートのサービスを組む唯一の場所（ADR-0081 §7）。worker と API が同じ関数を使う。

``AVP_QUALITY_GATE_ENFORCE`` が OFF のときは何も組み立てない（``None``。MinIO クライアントも
作らない / INV-130）。
BGM・地図の枝は ``build_quality_producers`` に ``CheckSetProducer`` を足すだけで gate に繋がる。
"""

from __future__ import annotations

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from domain.quality.records import CheckSetProducer
from infrastructure.config import Settings
from infrastructure.quality.service import QualityGateService
from infrastructure.storage.minio_store import MinioArtifactStore


def build_quality_producers() -> tuple[CheckSetProducer, ...]:
    """gate 時点に呼ぶ producer の列（技術 QA・説明欄は ``QualityGateService`` が持つ）。

    他の枝はここへ足す:

    - BGM: ``domain.music.quality_checks.bgm_quality_checks`` を包んだ ``CheckKey.BGM`` の producer
    - 地図: ``domain.geo.quality_checks.map_quality_checks`` を包んだ ``CheckKey.MAP`` の producer
    """
    return ()


def build_quality_service(
    settings: Settings, session_factory: async_sessionmaker[AsyncSession]
) -> QualityGateService | None:
    if not settings.quality_gate_enforce:
        return None
    return QualityGateService(
        session_factory=session_factory,
        store=MinioArtifactStore.from_settings(settings),
        producers=build_quality_producers(),
        destination_id=settings.youtube_channel_id,
    )
