"""upload smoke の補助（ADR-0020）。scripts/smoke-upload.sh から呼ぶ。外部呼び出しはしない。

- ``check-input EPISODE_ID`` : render_ready で現行 final_video があるか
- ``verify EPISODE_ID``      : PostgreSQL と MinIO **だけ**から投稿結果を検証し JSON 1行を出す
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from typing import Any, NoReturn

from sqlalchemy import text

from contracts.states import ArtifactType, EpisodeStatus, JobStatus, JobType
from contracts.upload import parse_upload_receipt
from domain.artifact.hashing import canonical_json_bytes, sha256_hex
from infrastructure.config import Settings
from infrastructure.db.repositories import (
    ArtifactMetadataRepository,
    EpisodeRepository,
    JobRepository,
)
from infrastructure.db.session import session_factory_from_settings
from infrastructure.storage.minio_store import MinioArtifactStore


def _fail(message: str) -> NoReturn:
    print(f"NG: {message}", file=sys.stderr)
    sys.exit(1)


async def check_input(episode_id: str) -> None:
    factory = session_factory_from_settings(Settings())
    async with factory() as session:
        episode = await EpisodeRepository(session).get(episode_id)
        final = await ArtifactMetadataRepository(session).find_current_by_type(
            episode_id, ArtifactType.FINAL_VIDEO
        )
    if episode is None or episode.status is not EpisodeStatus.RENDER_READY:
        _fail(f"episode {episode_id} is {episode.status.value if episode else None}")
    if final is None:
        _fail("no current final_video")


async def verify(episode_id: str) -> dict[str, Any]:
    settings = Settings()
    factory = session_factory_from_settings(settings)
    store = MinioArtifactStore.from_settings(settings)
    async with factory() as session:
        episode = await EpisodeRepository(session).get(episode_id)
        repo = ArtifactMetadataRepository(session)
        receipt_meta = await repo.find_current_by_type(episode_id, ArtifactType.UPLOAD_RECEIPT)
        final_meta = await repo.find_current_by_type(episode_id, ArtifactType.FINAL_VIDEO)
        jobs = [
            j
            for j in await JobRepository(session).list_for_episode(episode_id)
            if j.type is JobType.UPLOAD_FINAL_VIDEO
        ]
        reservations = (
            await session.execute(
                text(
                    "SELECT status, round, provider_result_ref, provider_job_ref, "
                    "outcome_artifact_id, dispatched_at, idempotency_key "
                    "FROM provider_reservations "
                    "WHERE episode_id = :e AND provider = 'youtube_upload'"
                ),
                {"e": episode_id},
            )
        ).all()
    if episode is None or episode.status is not EpisodeStatus.UPLOADED:
        _fail(f"episode status is {episode.status.value if episode else None}")
    if receipt_meta is None or final_meta is None:
        _fail("missing current upload_receipt or final_video")
    assert receipt_meta is not None and final_meta is not None
    if len(reservations) != 1:
        _fail(f"expected 1 youtube_upload reservation, got {len(reservations)}")
    res = reservations[0]
    if res.status != "spent" or not res.provider_result_ref:
        _fail(f"reservation status={res.status} result_ref set={bool(res.provider_result_ref)}")
    if res.round != 1:
        _fail(f"reservation round {res.round} != 1")
    succeeded = [j for j in jobs if j.status is JobStatus.SUCCEEDED]
    if len(succeeded) != 1:
        _fail(f"expected 1 succeeded upload job, got {[j.status.value for j in jobs]}")

    payload = await store.get_json(receipt_meta.object_key)
    if sha256_hex(canonical_json_bytes(payload)) != receipt_meta.sha256:
        _fail("receipt JSON sha256 != artifact_metadata.sha256")
    raw = json.dumps(payload)
    if "session" in raw.lower() or (res.provider_job_ref and res.provider_job_ref in raw):
        _fail("receipt mentions an upload session")
    receipt = parse_upload_receipt(payload)
    if receipt.privacy_status != "private" or receipt.metadata.privacy_status != "private":
        _fail("receipt is not private")
    if receipt.video_id != res.provider_result_ref:
        _fail("receipt video id != reservation provider_result_ref")
    if receipt.upload_key != res.idempotency_key:
        _fail("receipt upload_key != reservation idempotency_key")
    if str(res.outcome_artifact_id or "") not in ("", receipt_meta.id):
        _fail("reservation outcome_artifact_id != receipt artifact")
    if receipt.source_final_video.artifact_id != final_meta.id:
        _fail("receipt source_final_video is not the current final_video")
    return {
        "receipt_artifact": receipt_meta.id,
        "version": receipt_meta.version,
        "video_id": receipt.video_id,
        "privacy": receipt.privacy_status,
        "bytes": receipt.bytes,
        "reconciled_by": receipt.reconciled_by,
        "reservation": f"{res.status} round={res.round}",
        "upload_jobs": [j.status.value for j in jobs],
    }


async def _main() -> None:
    parser = argparse.ArgumentParser()
    sub = parser.add_subparsers(dest="cmd", required=True)
    sub.add_parser("check-input").add_argument("episode_id")
    sub.add_parser("verify").add_argument("episode_id")
    args = parser.parse_args()
    if args.cmd == "check-input":
        await check_input(args.episode_id)
    else:
        print(json.dumps(await verify(args.episode_id)))


if __name__ == "__main__":
    asyncio.run(_main())
