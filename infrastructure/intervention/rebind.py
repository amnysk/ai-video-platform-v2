"""「この storyboard が使う台本」の唯一の解決（ADR-0041 §6、``docs/domain/intervention.md`` §7）。

台本の文だけを直す変更（``script.edit_scenes``: ナレーション・字幕文・題・hook。
シーン構成と尺は不変）は storyboard を作り直さない。作り直すと ``storyboard_sha256`` を
含む全画像・動画の入力指紋が変わり、有料素材を全部作り直すため。代わりに
``revision_links(kind=rebound_to, upstream=新台本, downstream=storyboard)`` を残して、
storyboard を新しい台本へ結び直す。

production / voice / render / scene_recovery の「storyboard は現行台本から作られた」検査は
``bound_script_ref`` だけを通る。結び直しが無ければ、従来どおり ``storyboard.source_script``
（既存の Episode・日次実行の挙動は変わらない）。

結び直しの検査（毎回やり直す。link を書いた側を信用しない）:

- 結び直し先は同じ Episode の ``script`` revision であること
- 構成が同じこと: 台本シーンの id・並び・``duration_ms`` が storyboard の元の台本と一致
  （``check_rebound_script``）。storyboard の網羅（``check_storyboard_covers_script``）も満たすこと

違反は ``ProductionInputInvalidError``（needs_input。人が直す）。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Protocol

from sqlalchemy.ext.asyncio import AsyncSession

from contracts.artifacts import ScriptArtifact, StoryboardArtifact
from contracts.intervention import RevisionLinkKind
from contracts.states import ArtifactType
from domain.artifact.entities import ArtifactMetadata
from domain.artifact.hashing import canonical_json_bytes, sha256_hex
from domain.errors import (
    ProductionInputInvalidError,
    ProductionInputMissingError,
    StoryboardSchemaViolationError,
)
from domain.storyboard.coverage import check_storyboard_covers_script
from infrastructure.db.repositories import ArtifactMetadataRepository
from infrastructure.intervention.store import InterventionStore


class _JsonReader(Protocol):
    async def get_json(self, key: str) -> Any: ...


@dataclass(frozen=True, slots=True)
class ScriptRef:
    """storyboard が使う台本の固定（artifact_id / sha256）。"""

    artifact_id: str
    sha256: str
    #: True = ``revision_links(kind=rebound_to)`` で結び直した台本
    #: （``storyboard.source_script`` と違う）
    rebound: bool


async def bound_script_ref(
    session: AsyncSession, storyboard_meta: ArtifactMetadata, storyboard: StoryboardArtifact
) -> ScriptRef:
    """この storyboard revision が使う台本。結び直しが無ければ ``storyboard.source_script``。"""
    links = await InterventionStore(session).links(
        downstream_revision_id=storyboard_meta.id, kind=RevisionLinkKind.REBOUND_TO
    )
    if not links:
        source = storyboard.source_script
        return ScriptRef(artifact_id=source.artifact_id, sha256=source.sha256, rebound=False)
    latest = links[-1]  # created_at 順。最後の結び直しが現行
    meta = await ArtifactMetadataRepository(session).get(latest.upstream_revision_id)
    if meta is None:
        raise ProductionInputMissingError(
            f"script {latest.upstream_revision_id} rebound to storyboard {storyboard_meta.id} "
            "not found"
        )
    if meta.artifact_type is not ArtifactType.SCRIPT:
        raise ProductionInputInvalidError(
            f"revision {meta.id} rebound to storyboard {storyboard_meta.id} is not a script "
            f"({meta.artifact_type.value})"
        )
    if str(meta.episode_id) != str(storyboard_meta.episode_id):
        raise ProductionInputInvalidError(
            f"script {meta.id} rebound to storyboard {storyboard_meta.id} "
            "belongs to another episode"
        )
    return ScriptRef(artifact_id=meta.id, sha256=meta.sha256, rebound=True)


def _layout(script: ScriptArtifact) -> list[tuple[str, int]]:
    return [(scene.id, scene.duration_ms) for scene in script.scenes]


def check_rebound_script(original: ScriptArtifact, bound: ScriptArtifact) -> None:
    """結び直してよい台本か: シーンの id・並び・尺が元の台本と同じ（文だけの修正）。"""
    if _layout(original) != _layout(bound):
        raise ProductionInputInvalidError(
            "rebound script changes the scene structure (ids/order/durations): "
            f"{_layout(original)} -> {_layout(bound)}; the storyboard must be regenerated"
        )
    if original.episode_id != bound.episode_id:
        raise ProductionInputInvalidError("rebound script belongs to another episode")


async def verify_rebound(
    session: AsyncSession,
    store: _JsonReader,
    storyboard: StoryboardArtifact,
    bound: ScriptArtifact,
) -> None:
    """``ScriptRef.rebound`` のときに呼ぶ。元の台本を読み直して構成の一致と網羅を検査する。"""
    source = storyboard.source_script
    original_meta = await ArtifactMetadataRepository(session).get(source.artifact_id)
    if original_meta is None or original_meta.artifact_type is not ArtifactType.SCRIPT:
        raise ProductionInputMissingError(
            f"original script {source.artifact_id} of the rebound storyboard not found"
        )
    payload = await store.get_json(original_meta.object_key)
    digest = sha256_hex(canonical_json_bytes(payload))
    if digest != source.sha256 or original_meta.sha256 != source.sha256:
        raise ProductionInputInvalidError(
            f"original script {source.artifact_id} sha256 {digest} != storyboard source_script "
            f"{source.sha256}"
        )
    try:
        original = ScriptArtifact.model_validate(payload)
    except Exception as exc:  # pydantic ValidationError を含む
        raise ProductionInputInvalidError(f"original script invalid: {str(exc)[:300]}") from exc
    check_rebound_script(original, bound)
    try:
        check_storyboard_covers_script(storyboard, bound)
    except StoryboardSchemaViolationError as exc:
        raise ProductionInputInvalidError(f"rebound script structure: {exc}") from exc


__all__ = ["ScriptRef", "bound_script_ref", "check_rebound_script", "verify_rebound"]
