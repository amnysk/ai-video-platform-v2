"""説明欄に載せる BGM クレジットの唯一の読み口（ADR-0086 §5）。

upload 工程と品質ゲートの説明欄 lint は、この読み口の結果を同じく
``build_upload_metadata(credits=...)`` に渡す。lint した文と投稿した文が食い違わないための一点集約。
クレジットの原文は証跡の ``credit_lines``（カタログ原文。ここでは書き換えない）。
"""

from __future__ import annotations

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from contracts.upload import description_composition_enabled
from infrastructure.db.quality_repositories import QualityRecordRepository
from infrastructure.music.evidence import BgmEvidenceError, load_bgm_evidence
from infrastructure.storage.artifact_store import ArtifactStore


class BgmCreditSource:
    def __init__(
        self, store: ArtifactStore, session_factory: async_sessionmaker[AsyncSession]
    ) -> None:
        self._store = store
        self._session_factory = session_factory

    async def credit_lines(
        self, episode_id: str, *, final_video_sha256: str, media_sha256: str
    ) -> tuple[str, ...]:
        """現行 final_video（artifact の ``final_video_sha256``）に結び付く BGM のクレジット。

        - 説明欄の構成 flag が OFF なら ``()``（``build_upload_metadata`` が使わないので読まない）
        - BGM の証跡が無い（BGM 無しの動画）なら ``()``
        - 証跡があるのに読めない・現行の動画が入れ替わった場合は例外（黙って落とさない）
        """
        if not description_composition_enabled():
            return ()
        async with self._session_factory() as session:
            current = await QualityRecordRepository(session).current_final_video(episode_id)
        if current is None or current.final_video_sha256 != final_video_sha256:
            raise BgmEvidenceError("the current final_video changed while reading credits")
        loaded = await load_bgm_evidence(
            self._store,
            episode_id=episode_id,
            media_sha256=media_sha256,
            input_hash=current.input_hash,
        )
        return () if loaded is None else loaded[0].credit_lines
