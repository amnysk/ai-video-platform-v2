"""render smoke の補助（ADR-0019）。scripts/smoke-render.sh から呼ぶ。**有料呼び出しはしない。**

サブコマンド:

- ``seed``   : 合成の ``assets_ready`` Episode を通常の repository + ArtifactStore で作る。
  メディアは固定版 ffmpeg で生成した本物の mp4 / wav（尺は trim / freeze_tail / exact を踏む）。
  標準出力の最終行に Episode id を出す。
- ``check-input EPISODE_ID`` : 既存 Episode が render できる状態か（status と現行マニフェスト）。
- ``verify`` : PostgreSQL と MinIO **だけ**から完成動画を検証する。結果を JSON 1行で出す。
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Any, NoReturn

from sqlalchemy import text

from contracts.artifacts import (
    PRODUCTION_ARTIFACT_SCHEMA_VERSION,
    SCRIPT_ARTIFACT_SCHEMA_VERSION,
    STORYBOARD_ARTIFACT_SCHEMA_VERSION,
    build_scene_image_artifact,
    build_scene_video_artifact,
    build_scene_voice_artifact,
    build_script_artifact,
    build_storyboard_artifact,
    parse_final_video,
    parse_script_artifact,
    parse_storyboard_artifact,
)
from contracts.render import get_render_profile
from contracts.states import ArtifactType, EpisodeStatus, JobStatus, JobType
from domain.artifact.hashing import canonical_json_bytes, sha256_hex
from domain.artifact.keys import artifact_object_key, media_object_key
from domain.episode.transitions import EpisodeEvent
from domain.production.manifest import ArtifactRef, build_manifest
from infrastructure.config import Settings
from infrastructure.db.repositories import (
    ArtifactMetadataRepository,
    EpisodeRepository,
    JobRepository,
)
from infrastructure.db.session import session_factory_from_settings
from infrastructure.media.probe import PillowAvMediaProbe
from infrastructure.storage.artifact_store import readback_sha256
from infrastructure.storage.minio_store import MinioArtifactStore

TO_ASSETS_READY = [
    EpisodeEvent.WORKFLOW_STARTED,
    EpisodeEvent.SCRIPT_READY,
    EpisodeEvent.STAGE_ADMITTED,
    EpisodeEvent.STORYBOARD_READY,
    EpisodeEvent.STAGE_ADMITTED,
    EpisodeEvent.ASSETS_READY,
]

#: (script scene, storyboard 尺, 生成する動画の実尺, 音声尺, narration)
#: sb1: 実尺 > 目標 → trim / sb2: 1秒短い → freeze_tail / sb3: 一致 → exact
LAYOUT = [
    ("s1", 6000, 7000, 4500, "Clay pots still carry scorch marks. They tell us what people ate."),
    ("s2", 5000, 4000, 3800, "Hearths were lit inside the houses. Food was cooked every day!"),
    ("s3", 4000, 4000, 3000, "Stable food made settled villages possible."),
]
GENERATOR = {
    "generator": "smoke-render",
    "generator_model": "ffmpeg-lavfi",
    "generation_profile_id": "smoke-render-v1",
}


def _fail(message: str) -> NoReturn:
    print(f"NG: {message}", file=sys.stderr)
    sys.exit(1)


def _ffmpeg(*args: str) -> None:
    binary = os.environ.get("RENDER_FFMPEG_PATH") or _fail("RENDER_FFMPEG_PATH is not set")
    subprocess.run(
        [str(binary), "-hide_banner", "-loglevel", "error", "-y", *args],
        check=True,
    )


def _src(meta: Any) -> dict[str, str]:
    return {"artifact_id": meta.id, "sha256": meta.sha256, "schema_version": meta.schema_version}


class Seeder:
    def __init__(self) -> None:
        self.settings = Settings()
        self.factory = session_factory_from_settings(self.settings)
        self.store = MinioArtifactStore.from_settings(self.settings)
        self.bucket = self.settings.minio_bucket
        self.probe = PillowAvMediaProbe()

    async def record_json(
        self,
        episode_id: str,
        artifact_type: ArtifactType,
        schema_version: str,
        payload: dict[str, Any],
        scene_id: str | None = None,
    ) -> Any:
        digest = sha256_hex(canonical_json_bytes(payload))
        key = artifact_object_key(episode_id, artifact_type.value, digest, scene_id)
        put = await self.store.put_json(key, payload)
        async with self.factory() as session:
            meta = await ArtifactMetadataRepository(session).record(
                episode_id=episode_id,
                artifact_type=artifact_type,
                schema_version=schema_version,
                bucket=self.bucket,
                object_key=put.key,
                sha256=put.sha256,
                size_bytes=put.size,
                input_hash=sha256_hex(
                    f"smoke-render-input:{artifact_type.value}:{digest}".encode()
                ),
                scene_id=scene_id,
            )
            await session.commit()
        return meta

    async def put_media(
        self, episode_id: str, kind: str, scene_id: str, data: bytes, ext: str, mime: str
    ) -> dict[str, Any]:
        sha = sha256_hex(data)
        key = media_object_key(episode_id, kind, scene_id, sha, ext)
        await self.store.put_bytes(key, data, mime)
        if await readback_sha256(self.store, key) != sha:
            _fail(f"media readback mismatch {key}")
        return {"object_key": key, "sha256": sha, "bytes": len(data), "mime": mime}

    async def seed(self) -> str:
        await self.store.ensure_bucket()
        async with self.factory() as session:
            episodes = EpisodeRepository(session)
            episode = await episodes.create(topic="smoke render (synthetic)")
            await session.commit()
        ep = episode.id
        script = await self.record_json(
            ep,
            ArtifactType.SCRIPT,
            SCRIPT_ARTIFACT_SCHEMA_VERSION,
            build_script_artifact(
                episode_id=ep,
                language="en",
                title="What the clay pots remember",
                hook="Scorch marks tell a story",
                scenes=[
                    {"id": sid, "narration": text_, "visual": f"visual {sid}", "duration_ms": t}
                    for sid, t, _a, _v, text_ in LAYOUT
                ],
                metadata={"topic": "smoke", "generator": "smoke", "generator_model": "none"},
            ),
        )
        starts = [sum(row[1] for row in LAYOUT[:i]) for i in range(len(LAYOUT))]
        storyboard = await self.record_json(
            ep,
            ArtifactType.STORYBOARD,
            STORYBOARD_ARTIFACT_SCHEMA_VERSION,
            build_storyboard_artifact(
                episode_id=ep,
                source_script=_src(script),
                scenes=[
                    {
                        "scene_id": f"sb{i}",
                        "order": i,
                        "script_scene_id": sid,
                        "start_ms": starts[i - 1],
                        "duration_ms": t,
                        "visual_kind": "broll",
                        "visual_description": f"test pattern {i}",
                    }
                    for i, (sid, t, _a, _v, _n) in enumerate(LAYOUT, start=1)
                ],
                total_duration_ms=sum(row[1] for row in LAYOUT),
                metadata={
                    "generator": "smoke",
                    "generator_model": "none",
                    "generation_spec_id": "smoke-render",
                },
            ),
        )
        images: dict[str, ArtifactRef] = {}
        videos: dict[str, ArtifactRef] = {}
        voices: dict[str, ArtifactRef] = {}
        work_root = Path(self.settings.ai_video_work_root)
        work_root.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(prefix="smoke-render-", dir=work_root) as tmp:
            tmpdir = Path(tmp)
            for i, (sid, _t, actual_ms, voice_ms, _n) in enumerate(LAYOUT, start=1):
                scene_id = f"sb{i}"
                png = tmpdir / f"{scene_id}.png"
                mp4 = tmpdir / f"{scene_id}.mp4"
                wav = tmpdir / f"{sid}.wav"
                _ffmpeg(
                    "-f",
                    "lavfi",
                    "-i",
                    "testsrc2=size=720x1280:rate=30",
                    "-frames:v",
                    "1",
                    str(png),
                )
                _ffmpeg(
                    "-f", "lavfi", "-i", "testsrc2=size=720x1280:rate=30",
                    "-t", f"{actual_ms / 1000:.3f}", "-an",
                    "-c:v", "libx264", "-pix_fmt", "yuv420p", "-preset", "veryfast",
                    str(mp4),
                )  # fmt: skip
                # 声らしく: 基音をゆっくり揺らし、音節のような振幅の山を付ける（mono 22050Hz）
                _ffmpeg(
                    "-f", "lavfi",
                    "-i", (
                        "aevalsrc='0.4*sin(2*PI*(160+30*sin(2*PI*3*t))*t)"
                        "*(0.5+0.5*sin(2*PI*4*t))':s=22050:c=mono"
                    ),
                    "-t", f"{voice_ms / 1000:.3f}", "-c:a", "pcm_s16le",
                    str(wav),
                )  # fmt: skip
                image_bytes = png.read_bytes()
                image_info = self.probe.probe_image(image_bytes)
                image_meta = await self.record_json(
                    ep,
                    ArtifactType.SCENE_IMAGE,
                    PRODUCTION_ARTIFACT_SCHEMA_VERSION,
                    build_scene_image_artifact(
                        episode_id=ep,
                        source_storyboard=_src(storyboard),
                        scene_id=scene_id,
                        media=await self.put_media(
                            ep, "scene_image", scene_id, image_bytes, "png", "image/png"
                        ),
                        width=image_info.width,
                        height=image_info.height,
                        generator=GENERATOR,
                    ),
                    scene_id=scene_id,
                )
                images[scene_id] = ArtifactRef(image_meta.id, image_meta.sha256)

                video_bytes = mp4.read_bytes()
                vinfo = self.probe.probe_video(video_bytes)
                video_meta = await self.record_json(
                    ep,
                    ArtifactType.SCENE_VIDEO,
                    PRODUCTION_ARTIFACT_SCHEMA_VERSION,
                    build_scene_video_artifact(
                        episode_id=ep,
                        source_storyboard=_src(storyboard),
                        scene_id=scene_id,
                        source_image={"artifact_id": image_meta.id, "sha256": image_meta.sha256},
                        media=await self.put_media(
                            ep, "scene_video", scene_id, video_bytes, "mp4", "video/mp4"
                        ),
                        duration_ms=vinfo.duration_ms,
                        requested_duration_ms=LAYOUT[i - 1][1],
                        width=vinfo.width,
                        height=vinfo.height,
                        fps_millis=vinfo.fps_millis,
                        generator=GENERATOR,
                    ),
                    scene_id=scene_id,
                )
                videos[scene_id] = ArtifactRef(video_meta.id, video_meta.sha256)

                wav_bytes = wav.read_bytes()
                ainfo = self.probe.probe_audio(wav_bytes)
                voice_meta = await self.record_json(
                    ep,
                    ArtifactType.SCENE_VOICE,
                    PRODUCTION_ARTIFACT_SCHEMA_VERSION,
                    build_scene_voice_artifact(
                        episode_id=ep,
                        source_storyboard=_src(storyboard),
                        source_script=_src(script),
                        script_scene_id=sid,
                        storyboard_scene_ids=[scene_id],
                        language="en",
                        voice_id="smoke-sine",
                        media=await self.put_media(
                            ep, "scene_voice", sid, wav_bytes, "wav", "audio/wav"
                        ),
                        duration_ms=ainfo.duration_ms,
                        sample_rate_hz=ainfo.sample_rate_hz,
                        channels=ainfo.channels,
                        generator=GENERATOR,
                    ),
                    scene_id=sid,
                )
                voices[sid] = ArtifactRef(voice_meta.id, voice_meta.sha256)
                print(
                    f"  seeded {scene_id}/{sid}: video {vinfo.duration_ms}ms "
                    f"(storyboard {LAYOUT[i - 1][1]}ms) voice {ainfo.duration_ms}ms",
                    file=sys.stderr,
                )

        manifest = await self.record_json(
            ep,
            ArtifactType.PRODUCTION_MANIFEST,
            PRODUCTION_ARTIFACT_SCHEMA_VERSION,
            build_manifest(
                episode_id=ep,
                storyboard_ref=ArtifactRef(storyboard.id, storyboard.sha256),
                script_ref=ArtifactRef(script.id, script.sha256),
                storyboard=parse_storyboard_artifact(
                    await self.store.get_json(storyboard.object_key)
                ),
                script=parse_script_artifact(await self.store.get_json(script.object_key)),
                images=images,
                videos=videos,
                voices=voices,
            ),
        )
        async with self.factory() as session:
            episodes = EpisodeRepository(session)
            for event in TO_ASSETS_READY:
                await episodes.apply_event(ep, event)
            await session.commit()
        print(f"  manifest {manifest.id}", file=sys.stderr)
        return ep

    async def check_input(self, episode_id: str) -> None:
        async with self.factory() as session:
            episode = await EpisodeRepository(session).get(episode_id)
            if episode is None:
                _fail(f"episode {episode_id} not found")
            assert episode is not None
            manifest = await ArtifactMetadataRepository(session).find_current_by_type(
                episode_id, ArtifactType.PRODUCTION_MANIFEST
            )
        if episode.status not in (EpisodeStatus.ASSETS_READY, EpisodeStatus.RENDER_READY):
            _fail(
                f"episode {episode_id} is {episode.status.value} (need assets_ready/render_ready)"
            )
        if manifest is None:
            _fail(f"episode {episode_id} has no current production_manifest")

    async def verify(
        self, episode_id: str, profile_id: str, expect: str, previous: str | None
    ) -> dict[str, Any]:
        profile = get_render_profile(profile_id)
        async with self.factory() as session:
            episode = await EpisodeRepository(session).get(episode_id)
            repo = ArtifactMetadataRepository(session)
            current = await repo.find_current_by_type(episode_id, ArtifactType.FINAL_VIDEO)
            jobs = [
                j
                for j in await JobRepository(session).list_for_episode(episode_id)
                if j.type is JobType.RENDER_FINAL_VIDEO
            ]
            row = (
                await session.execute(
                    text(
                        "SELECT input_hash, produced_by_job_id, superseded_at, size_bytes "
                        "FROM artifact_metadata WHERE id = :id"
                    ),
                    {"id": current.id if current else None},
                )
            ).one_or_none()
            prev_row = None
            if previous:
                prev_row = (
                    await session.execute(
                        text("SELECT version, superseded_at FROM artifact_metadata WHERE id = :id"),
                        {"id": previous},
                    )
                ).one_or_none()
        if episode is None or episode.status is not EpisodeStatus.RENDER_READY:
            _fail(f"episode status is {episode.status.value if episode else None}")
        if current is None or row is None:
            _fail("no current final_video")
        assert current is not None and row is not None
        if not jobs:
            _fail("no render_final_video job")
        latest = max(jobs, key=lambda j: j.created_at)
        want_job = JobStatus.SKIPPED if expect == "skipped" else JobStatus.SUCCEEDED
        if latest.status is not want_job:
            _fail(f"latest render job is {latest.status.value}, expected {want_job.value}")
        if not row.input_hash or row.superseded_at is not None:
            _fail("current final_video row lacks input_hash or is superseded")

        payload = await self.store.get_json(current.object_key)
        if sha256_hex(canonical_json_bytes(payload)) != current.sha256:
            _fail("final_video JSON sha256 != artifact_metadata.sha256")
        final = parse_final_video(payload)
        if final.render_profile.profile_id != profile_id:
            _fail(f"final_video profile {final.render_profile.profile_id} != {profile_id}")
        streamed = await readback_sha256(self.store, final.media.object_key)
        if streamed != final.media.sha256:
            _fail("final media streamed sha256 != descriptor")
        if not final.technical_qa.passed or not all(c.passed for c in final.technical_qa.checks):
            _fail("technical QA not all passed")

        work_root = Path(self.settings.ai_video_work_root)
        with tempfile.TemporaryDirectory(prefix="smoke-render-verify-", dir=work_root) as tmp:
            path = Path(tmp) / "final.mp4"
            path.write_bytes(await self.store.get_bytes(final.media.object_key))
            info = self.probe.probe_final_video(str(path))
        problems = []
        if (info.width, info.height) != (profile.width, profile.height):
            problems.append(f"resolution {info.width}x{info.height}")
        if abs(info.fps_millis - profile.fps_millis) > 1:
            problems.append(f"fps {info.fps_millis}")
        if info.video_codec != profile.video.codec or info.pix_fmt != profile.video.pix_fmt:
            problems.append(f"video {info.video_codec}/{info.pix_fmt}")
        if not info.audio_present or info.audio_codec != profile.audio.codec:
            problems.append(f"audio {info.audio_present}/{info.audio_codec}")
        if (info.audio_sample_rate_hz, info.audio_channels) != (
            profile.audio.sample_rate_hz,
            profile.audio.channels,
        ):
            problems.append(f"audio {info.audio_sample_rate_hz}Hz/{info.audio_channels}ch")
        if abs(info.duration_ms - final.total_duration_ms) > profile.limits.duration_tolerance_ms:
            problems.append(f"duration {info.duration_ms} vs plan {final.total_duration_ms}")
        if info.decode_errors or info.frames_decoded <= 0:
            problems.append(f"decode errors={info.decode_errors} frames={info.frames_decoded}")
        if info.bytes != final.media.bytes:
            problems.append(f"bytes {info.bytes} != {final.media.bytes}")
        if problems:
            _fail("probe mismatch: " + "; ".join(problems))

        if expect == "skipped":
            if previous != current.id:
                _fail(f"skip did not reuse artifact {previous} (current {current.id})")
        elif previous:
            if prev_row is None or prev_row.superseded_at is None:
                _fail(f"previous final_video {previous} is not superseded")
            if current.id == previous or current.version != prev_row.version + 1:
                _fail(f"new version expected (prev v{prev_row.version}, now v{current.version})")

        return {
            "artifact_id": current.id,
            "version": current.version,
            "job": f"{latest.id}:{latest.status.value}",
            "profile": profile_id,
            "measured": f"{info.width}x{info.height}@{info.fps_millis / 1000:g} "
            f"{info.video_codec}/{info.pix_fmt} {info.audio_codec} "
            f"{info.audio_sample_rate_hz}Hz/{info.audio_channels}ch",
            "duration_ms": info.duration_ms,
            "plan_total_ms": final.total_duration_ms,
            "audio_duration_ms": info.audio_duration_ms,
            "bytes": final.media.bytes,
            "media_sha256": final.media.sha256,
            "timeline": [f"{s.scene_id}:{s.reconciliation.mode}" for s in final.timeline],
            "subtitle_cues": len(final.subtitle_cues),
            "qa_checks": len(final.technical_qa.checks),
            "previous_superseded_at": (
                prev_row.superseded_at.isoformat() if prev_row and prev_row.superseded_at else None
            ),
        }


async def _main() -> None:
    parser = argparse.ArgumentParser()
    sub = parser.add_subparsers(dest="cmd", required=True)
    sub.add_parser("seed")
    check = sub.add_parser("check-input")
    check.add_argument("episode_id")
    verify = sub.add_parser("verify")
    verify.add_argument("episode_id")
    verify.add_argument("--profile", required=True)
    verify.add_argument("--expect", choices=("rendered", "skipped"), required=True)
    verify.add_argument("--previous")
    args = parser.parse_args()
    seeder = Seeder()
    if args.cmd == "seed":
        print(await seeder.seed())
    elif args.cmd == "check-input":
        await seeder.check_input(args.episode_id)
    else:
        result = await seeder.verify(args.episode_id, args.profile, args.expect, args.previous)
        print(json.dumps(result))


if __name__ == "__main__":
    asyncio.run(_main())
