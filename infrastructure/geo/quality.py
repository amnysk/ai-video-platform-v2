"""地図の検査セットの producer（ADR-0086 §4）。``map_quality_checks`` を包むだけ。

地図の純関数・契約を知るのはこの窓口（``infrastructure/geo``）だけ（ADR-0084）。
gate は render の使用宣言が used のときだけ呼ぶので、証跡が無い・読めない・別の動画のものは例外
（記録されない = REVIEW）。拒否された地図は証跡に残り、``map_quality_checks`` が BLOCK にする。
"""

from __future__ import annotations

from datetime import datetime

from contracts.geo_scene import GeoRenderEvidence
from contracts.quality_records import CheckKey
from domain.artifact.hashing import canonical_json_bytes, sha256_hex
from domain.geo.quality_checks import map_quality_checks
from domain.quality.records import CheckSetDraft, ProducerContext
from infrastructure.geo.service import geo_evidence_key
from infrastructure.storage.artifact_store import ArtifactStore

PRODUCER = "map-quality"
PRODUCER_VERSION = "map-quality-1"


class GeoEvidenceError(RuntimeError):
    """地図の証跡が無い・読めない・別の動画のもの。"""


class MapCheckProducer:
    key = CheckKey.MAP

    def __init__(self, store: ArtifactStore) -> None:
        self._store = store

    async def produce(self, context: ProducerContext, *, now: datetime) -> CheckSetDraft | None:
        final = await context.final_video()
        subject = context.subject
        media_sha = final.media.sha256
        key = geo_evidence_key(subject.episode_id, media_sha, subject.input_hash)
        try:
            payload = await self._store.get_json(key)
        except KeyError as exc:
            raise GeoEvidenceError("map was declared used but its evidence is missing") from exc
        try:
            evidence = GeoRenderEvidence.model_validate(payload)
        except ValueError as exc:
            raise GeoEvidenceError("map evidence does not satisfy its contract") from exc
        if (
            evidence.episode_id != subject.episode_id
            or evidence.input_hash != subject.input_hash
            or evidence.final_video_sha256 != media_sha
        ):
            raise GeoEvidenceError("map evidence belongs to another video")
        checks = map_quality_checks(
            final_video_sha256=media_sha,
            records=evidence.records,
            intended_scene_ids=evidence.intended_scene_ids,
        )
        return CheckSetDraft(
            key=self.key,
            checks=checks,
            producer=PRODUCER,
            producer_version=PRODUCER_VERSION,
            evidence_ref=sha256_hex(canonical_json_bytes(payload)),
        )
