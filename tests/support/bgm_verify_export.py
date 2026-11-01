"""BGM の実素材検証（ADR-0083 §7）: 本番 Episode の読み取り専用 export を、本番の外で描いて測る。

本番の描画経路（``RenderActivities`` + 固定版 ffmpeg + 本物の probe）をそのまま使う。違いは
保存先だけ: SQLite（メモリ）+ メモリ ArtifactStore + 作業ディレクトリで、
PostgreSQL・MinIO・Temporal・
fal には一切つながない。第三者の音源は使わず、``tests/support/bgm_synth.py`` が合成した曲を使う。

使い方::

    uv run --extra dev python -m tests.support.bgm_verify_export EXPORT_DIR OUT_DIR [--ffmpeg PATH]

EXPORT_DIR: ``artifact JSON（script/ storyboard/ production_manifest/ scene_*/）`` と
``media/<episode_id>/...`` を持つ読み取り専用のコピー。OUT_DIR に mp4・wav・測定レポートを書く。
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import re
import subprocess
import sys
import tempfile
import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

from contracts.music import BgmTrack
from contracts.render_activities import RenderAdmitRequest, RenderFinalVideoRequest
from contracts.states import ArtifactType
from domain.artifact.hashing import canonical_json_bytes, sha256_hex
from domain.artifact.keys import artifact_object_key
from domain.music.intelligibility import evaluate_speech_intelligibility
from domain.music.placement import SceneRole, plan_cue_sheet
from domain.render.bgm import resolve_bgm
from domain.render.ports import BgmRenderInput
from infrastructure.db.models import ArtifactMetadataRow, Base, EpisodeRow, ProviderReservationRow
from infrastructure.media.probe import PillowAvMediaProbe
from infrastructure.music.catalog_store import register_track, write_cue_sheet
from infrastructure.render.bgm_intelligibility import FfmpegSpeechMarginProbe
from infrastructure.render.bgm_source import LocalDirectoryBgmSource
from infrastructure.render.binary import file_sha256
from infrastructure.render.ffmpeg_engine import FfmpegRenderEngine
from infrastructure.render.loudness import FfmpegLoudnessProbe
from infrastructure.storage.memory_store import InMemoryArtifactStore
from infrastructure.workdir import WorkDirectory
from tests.support.bgm_synth import synthesize
from workers.render.activities import RenderActivities

DEFAULT_FFMPEG = Path.home() / ".local/share/avp/ffmpeg/7.1.1/ffmpeg"
FFMPEG_SHA256 = "810f94020e76e2b58fb44759a322e86bea5d213ebededad7471f3a15b0bf2c5c"
FONT = Path("/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc")
BUCKET = "artifacts"
TRACK_ID = "bgm-selfgen-ambient-v1"
TRACK_SECONDS = 96


def _json(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def _ff(ffmpeg: Path, *args: str) -> str:
    done = subprocess.run(  # noqa: S603
        [str(ffmpeg), "-hide_banner", "-nostdin", "-nostats", *args],
        capture_output=True,
        text=True,
        check=False,
    )
    return done.stderr + done.stdout


def ebur128(ffmpeg: Path, path: Path, *, start: float | None = None, dur: float | None = None):
    cmd = ["-i", str(path)]
    if start is not None:
        cmd = ["-ss", f"{start:.3f}", "-t", f"{dur:.3f}", *cmd]
    out = _ff(ffmpeg, *cmd, "-vn", "-af", "ebur128=peak=true", "-f", "null", "-")
    tail = out[out.rfind("Summary:") :]

    def grab(label: str, unit: str) -> float | None:
        m = re.search(rf"{label}:\s*(-?inf|-?[0-9.]+) {unit}", tail)
        return None if m is None or m.group(1).endswith("inf") else float(m.group(1))

    return {
        "integrated_lufs": grab(r"I", "LUFS"),
        "lra_lu": grab(r"LRA", "LU"),
        "true_peak_dbfs": grab(r"Peak", "dBFS"),
    }


def probe_streams(ffmpeg: Path, path: Path) -> dict[str, Any]:
    ffprobe = ffmpeg.with_name("ffprobe")
    done = subprocess.run(  # noqa: S603
        [str(ffprobe), "-v", "error", "-show_streams", "-show_format", "-of", "json", str(path)],
        capture_output=True,
        text=True,
        check=True,
    )
    data = json.loads(done.stdout)
    out: dict[str, Any] = {"format_duration_s": float(data["format"]["duration"])}
    for s in data["streams"]:
        out[s["codec_type"]] = {
            "codec": s["codec_name"],
            "start_time_s": float(s.get("start_time", 0)),
            "duration_s": float(s.get("duration", 0)),
        }
    return out


def video_md5(ffmpeg: Path, path: Path) -> str:
    out = _ff(ffmpeg, "-i", str(path), "-map", "0:v:0", "-f", "md5", "-")
    m = re.search(r"MD5=([0-9a-f]{32})", out)
    return m.group(1) if m else "?"


async def seed(factory, store: InMemoryArtifactStore, export: Path) -> tuple[str, dict[str, Path]]:
    """export の artifact JSON と media を、本物の id・sha256 のまま SQLite / メモリへ置く。"""
    manifest_path = next((export / "production_manifest").glob("*.json"))
    manifest = _json(manifest_path)
    episode_id = manifest["episode_id"]
    ids: dict[str, str] = {
        manifest["source_script"]["sha256"]: manifest["source_script"]["artifact_id"],
        manifest["source_storyboard"]["sha256"]: manifest["source_storyboard"]["artifact_id"],
    }
    for scene in manifest["scenes"]:
        ids[scene["video"]["sha256"]] = scene["video"]["artifact_id"]
    for voice in manifest["voices"]:
        ids[voice["sha256"]] = voice["artifact_id"]
    voice_wavs: dict[str, Path] = {}
    async with factory() as session:
        session.add(
            EpisodeRow(id=uuid.UUID(episode_id), status="assets_ready", topic="bgm verification")
        )
        await session.flush()
        entries: list[tuple[ArtifactType, Path, str | None]] = [
            (ArtifactType.SCRIPT, next((export / "script").glob("*.json")), None),
            (ArtifactType.STORYBOARD, next((export / "storyboard").glob("*.json")), None),
            (ArtifactType.PRODUCTION_MANIFEST, manifest_path, None),
        ]
        for kind, atype in (("scene_video", ArtifactType.SCENE_VIDEO),
                            ("scene_voice", ArtifactType.SCENE_VOICE)):  # fmt: skip
            for scene_dir in sorted((export / kind).iterdir()):
                for f in sorted(scene_dir.glob("*.json")):
                    entries.append((atype, f, scene_dir.name))
        for atype, path, scene_id in entries:
            payload = _json(path)
            digest = sha256_hex(canonical_json_bytes(payload))
            if digest != path.stem:
                raise SystemExit(f"export file {path} does not match its own sha256")
            key = artifact_object_key(episode_id, atype.value, digest, scene_id)
            put = await store.put_json(key, payload)
            session.add(
                ArtifactMetadataRow(
                    id=uuid.UUID(ids.get(digest, str(uuid.uuid4()))),
                    episode_id=uuid.UUID(episode_id),
                    artifact_type=atype.value,
                    schema_version=payload.get("schema_version", "1.0"),
                    bucket=BUCKET,
                    object_key=put.key,
                    sha256=digest,
                    input_hash=sha256_hex(f"export:{digest}".encode()),
                    version=1,
                    size_bytes=put.size,
                    scene_id=scene_id,
                )
            )
            media = payload.get("media")
            if media and atype in (ArtifactType.SCENE_VIDEO, ArtifactType.SCENE_VOICE):
                data = (export / media["object_key"]).read_bytes()
                if hashlib.sha256(data).hexdigest() != media["sha256"]:
                    raise SystemExit(f"media sha256 mismatch for {media['object_key']}")
                await store.put_bytes(media["object_key"], data, media["mime"])
                if atype is ArtifactType.SCENE_VOICE:
                    voice_wavs[scene_id or ""] = export / media["object_key"]
        await session.commit()
    return episode_id, voice_wavs


async def build_plan(act: RenderActivities, episode_id: str, engine: FfmpegRenderEngine):
    """本番の Activity と同じ入力の読み方・同じ関数で ``RenderPlan`` を組む。"""
    from contracts.render import RENDER_PROFILES
    from domain.render.plan import build_render_plan

    inputs = await act._load_inputs(episode_id)  # noqa: SLF001
    return build_render_plan(
        manifest=inputs.manifest,
        script=inputs.script,
        storyboard=inputs.storyboard,
        voices=inputs.voices,
        videos=inputs.videos,
        profile=RENDER_PROFILES["shorts_vertical"],
        policy=act._policy,  # noqa: SLF001
        engine=engine.identity(),
    )


def ebur128_text(ffmpeg: Path, path: Path) -> str:
    out = _ff(ffmpeg, "-i", str(path), "-vn", "-af", "ebur128=peak=true:framelog=quiet",
              "-f", "null", "-")  # fmt: skip
    return out[out.rfind("Summary:") :]


def speech_and_gap_levels(probe: FfmpegSpeechMarginProbe, plan, music_wav: Path) -> dict[str, Any]:
    """BGM（ducking 後）のラウドネスを、発話区間と発話の無い区間で別々に測る。"""
    spans = sorted((v.start_ms, v.start_ms + v.duration_ms) for v in plan.voices)
    gaps: list[tuple[int, int]] = []
    cursor = 0
    for start, end in spans:
        if start - cursor >= 500:
            gaps.append((cursor, start))
        cursor = max(cursor, end)
    if plan.total_duration_ms - cursor >= 500:
        gaps.append((cursor, plan.total_duration_ms))
    speech = [
        {"start_ms": a, "end_ms": b, "music_lufs": _centi(probe.window_lufs(music_wav, a, b - a))}
        for a, b in spans
    ]
    quiet = [
        {"start_ms": a, "end_ms": b, "music_lufs": _centi(probe.window_lufs(music_wav, a, b - a))}
        for a, b in gaps
    ]
    return {"speech_windows": speech, "gap_windows": quiet}


def _centi(value: int | None) -> float | None:
    return None if value is None else value / 100


def write_excerpts(ffmpeg: Path, voice_wav: Path, with_bgm: Path, without: Path, out: Path):
    """ナレーションが最も詰まった 15 秒（silencedetect で発話が最大の窓）を wav にする。"""
    text = _ff(
        ffmpeg, "-i", str(voice_wav), "-af", "silencedetect=noise=-40dB:d=0.25", "-f", "null", "-"
    )
    starts = [float(x) for x in re.findall(r"silence_start: (-?[0-9.]+)", text)]
    ends = [float(x) for x in re.findall(r"silence_end: (-?[0-9.]+)", text)]
    total = 42.0
    silences = list(zip(starts, ends + [total] * (len(starts) - len(ends)), strict=False))

    def speaking(t0: float) -> float:
        t1 = t0 + 15.0
        quiet = sum(max(0.0, min(b, t1) - max(a, t0)) for a, b in silences)
        return 15.0 - quiet

    best = max((t / 2 for t in range(0, int((total - 15.0) * 2) + 1)), key=speaking)
    for name, src in (("with_bgm", with_bgm), ("without_bgm", without)):
        _ff(ffmpeg, "-y", "-ss", f"{best:.2f}", "-t", "15", "-i", str(src), "-vn", "-ac", "2",
            "-ar", "48000", "-c:a", "pcm_s16le", str(out / f"excerpt_15s_{name}.wav"))  # fmt: skip
    return {"start_s": best, "duration_s": 15.0, "speaking_s": round(speaking(best), 2)}


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("export", type=Path)
    ap.add_argument("out", type=Path)
    ap.add_argument("--ffmpeg", type=Path, default=DEFAULT_FFMPEG)
    args = ap.parse_args()
    asyncio.run(run(args.export.resolve(), args.out.resolve(), args.ffmpeg))


async def run(export: Path, out: Path, ffmpeg: Path) -> None:
    out.mkdir(parents=True, exist_ok=True)
    work = Path(tempfile.mkdtemp(prefix="bgm-verify-"))
    engine_db = create_async_engine(
        "sqlite+aiosqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool
    )
    async with engine_db.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine_db, expire_on_commit=False)
    store = InMemoryArtifactStore()
    episode_id, voice_wavs = await seed(factory, store, export)

    # --- 合成した曲を登録（原本は content-addressed、ラウドネスは実測して記録）
    bgm_root = work / "bgm"
    audio = synthesize(TRACK_SECONDS)
    track_sha = hashlib.sha256(audio).hexdigest()
    track_file = work / "synth.wav"
    track_file.write_bytes(audio)
    loud = FfmpegLoudnessProbe(ffmpeg).measure_loudness(str(track_file))
    evidence = (
        b"self-generated test material: synthesized by tests/support/bgm_synth.py "
        b"(standard library only). No third-party audio. For verification only; "
        b"not a catalog track and not for publication."
    )
    now = datetime.now(UTC)
    track = BgmTrack(
        track_id=TRACK_ID,
        file_sha256=track_sha,
        object_key=f"music/originals/{track_sha}.wav",
        title="Synthesized ambient pad (verification)",
        artist="avp2 test synthesizer",
        source_url="https://example.invalid/self-generated",
        source_kind="original",
        license_type="custom",
        license_status="approved",
        license_evidence_sha256=hashlib.sha256(evidence).hexdigest(),
        license_checked_at=now - timedelta(days=1),
        allowed_platforms=("youtube",),
        allowed_formats=("shorts", "longform"),
        commercial_use_allowed=True,
        reuse_allowed=True,
        modification_allowed=True,
        attribution_required=False,
        mood_tags=("mysterious", "calm"),
        genre_tags=("ambient",),
        energy="low",
        bpm=80,
        duration_ms=TRACK_SECONDS * 1000,
        loudness_lufs=(loud.integrated_lufs_centi or 0) / 100,
        status="active",
    )
    first = register_track(bgm_root, track, audio)
    again = register_track(bgm_root, track, audio)  # 再取込は DUPLICATE（再利用）
    (out / "license_evidence.txt").write_bytes(evidence)
    (out / "synth_track.wav").write_bytes(audio)

    # --- fal を呼ばないことの検査（呼ばれたら失敗 + 回数を数える）
    fal_calls: list[str] = []
    from infrastructure.providers import fal_queue, fal_storage

    def spy(name: str):
        async def _fail(*a: object, **k: object):
            fal_calls.append(name)
            raise AssertionError(f"fal called: {name}")

        return _fail

    for cls in (fal_queue.FalQueueClient, fal_storage.FalStorageClient):
        for attr in ("submit", "status", "result", "download", "upload", "upload_file"):
            if hasattr(cls, attr):
                setattr(cls, attr, spy(f"{cls.__name__}.{attr}"))

    render_engine = FfmpegRenderEngine.from_binary(ffmpeg, FFMPEG_SHA256)
    media_probe = PillowAvMediaProbe()
    loud_probe = FfmpegLoudnessProbe(ffmpeg)
    source = LocalDirectoryBgmSource(bgm_root)

    def activities(bgm: bool) -> RenderActivities:
        return RenderActivities(
            session_factory=factory,
            store=store,
            bucket=BUCKET,
            workdir=WorkDirectory(work / "render-work", forbidden=()),
            engine=render_engine,
            probe=media_probe,
            source_probe=media_probe,
            render_threads=4,
            font_path=FONT,
            font_sha256=file_sha256(FONT),
            render_timeout_seconds=900,
            min_free_bytes=1_000,
            bgm_source=source if bgm else None,
            loudness_probe=loud_probe if bgm else None,
        )

    async def production_state() -> dict[str, Any]:
        async with factory() as session:
            rows = (
                await session.execute(
                    select(ArtifactMetadataRow.id, ArtifactMetadataRow.sha256).where(
                        ArtifactMetadataRow.artifact_type != ArtifactType.FINAL_VIDEO.value
                    )
                )
            ).all()
            reservations = await session.scalar(
                select(func.count()).select_from(ProviderReservationRow)
            )
        return {"artifacts": sorted((str(i), s) for i, s in rows), "reservations": reservations}

    async def final_row() -> ArtifactMetadataRow:
        async with factory() as session:
            return (
                await session.scalars(
                    select(ArtifactMetadataRow).where(
                        ArtifactMetadataRow.artifact_type == ArtifactType.FINAL_VIDEO.value,
                        ArtifactMetadataRow.superseded_at.is_(None),
                    )
                )
            ).one()

    async def save_current(name: str) -> tuple[Path, dict[str, Any]]:
        row = await final_row()
        payload = await store.get_json(row.object_key)
        path = out / name
        path.write_bytes(await store.get_bytes(payload["media"]["object_key"]))
        return path, {
            "input_hash": row.input_hash,
            "final_video_sha256": payload["media"]["sha256"],
        }

    wf = "bgm-verify"
    before = await production_state()
    results: dict[str, Any] = {"episode_id": episode_id, "track": track.model_dump(mode="json")}
    results["register_first"] = first.outcome.value
    results["register_again"] = again.outcome.value
    request = RenderFinalVideoRequest(episode_id, wf, "run-1", "shorts_vertical")

    # --- A: BGM 無し（現行と同じ経路）
    act_off = activities(False)
    await act_off.admit(RenderAdmitRequest(episode_id, wf, "run-1"))
    await act_off.render_final_video(request)
    path_a, meta_a = await save_current("episode_without_bgm.mp4")

    # --- 計画（本番と同じ経路で RenderPlan を組む）
    act_on = activities(True)
    plan = await build_plan(act_on, episode_id, render_engine)
    roles_main = {
        "s1": SceneRole.INTRO,
        "s2": SceneRole.BODY,
        "s3": SceneRole.BODY,
        "s4": SceneRole.CLIMAX,
        "s5": SceneRole.OUTRO,
    }
    placed = plan_cue_sheet(plan, track, roles=roles_main)
    assert placed.cue_sheet is not None, placed.refusal
    write_cue_sheet(bgm_root, episode_id, placed.cue_sheet)

    # --- B: BGM 入り
    await act_on.render_final_video(request)
    path_b, meta_b = await save_current("episode_with_bgm.mp4")
    after_b = await production_state()

    # --- C: BGM だけ変更（s3 を無音・s4 を intro 並みに）→ render だけが走る
    roles_alt = {**roles_main, "s3": SceneRole.SILENT, "s4": SceneRole.BODY}
    alt = plan_cue_sheet(plan, track, roles=roles_alt)
    assert alt.cue_sheet is not None
    write_cue_sheet(bgm_root, episode_id, alt.cue_sheet)
    await act_on.render_final_video(request)
    path_c, meta_c = await save_current("episode_with_bgm_alt_placement.mp4")
    after_c = await production_state()

    # 元の本番動画（読み取り専用 export）
    original = next((export / "media").rglob("final_video/*.mp4"))

    # --- 実測
    resolved = resolve_bgm(
        placed.cue_sheet,
        [track],
        plan=plan,
        now=datetime.now(UTC),
        file_sha256_by_track={TRACK_ID: track_sha},
    )
    assert resolved.applied and resolved.plan is not None
    bgm_input = BgmRenderInput(
        mix=resolved.plan, track_paths={TRACK_ID: bgm_root / track.object_key}
    )
    probe = FfmpegSpeechMarginProbe(ffmpeg)
    stems = out / "stems"
    voice_wav, music_wav = probe.write_stems(plan, bgm_input, voice_wavs, stems)
    levels = probe.measure(plan, bgm_input, voice_wavs)
    intelligibility = evaluate_speech_intelligibility(levels)
    windows = speech_and_gap_levels(probe, plan, music_wav)
    excerpt = write_excerpts(ffmpeg, voice_wav, path_a, path_b, out)
    for name, path in (("original", original), ("without_bgm", path_a), ("with_bgm", path_b)):
        (out / f"ebur128_{name}.txt").write_text(ebur128_text(ffmpeg, path), encoding="utf-8")

    results.update(
        {
            "plan_total_ms": plan.total_duration_ms,
            "cue_sheet": json.loads(placed.cue_sheet.model_dump_json()),
            "mix_plan_sha256": resolved.mix_plan_sha256,
            "original": {
                "loudness": ebur128(ffmpeg, original),
                "streams": probe_streams(ffmpeg, original),
            },
            "without_bgm": {
                **meta_a,
                "loudness": ebur128(ffmpeg, path_a),
                "streams": probe_streams(ffmpeg, path_a),
                "video_md5": video_md5(ffmpeg, path_a),
            },
            "with_bgm": {
                **meta_b,
                "loudness": ebur128(ffmpeg, path_b),
                "streams": probe_streams(ffmpeg, path_b),
                "video_md5": video_md5(ffmpeg, path_b),
            },
            "with_bgm_alt_placement": {
                **meta_c,
                "loudness": ebur128(ffmpeg, path_c),
                "streams": probe_streams(ffmpeg, path_c),
                "alt_cues": json.loads(alt.cue_sheet.model_dump_json()),
            },
            "speech_intelligibility": json.loads(intelligibility.model_dump_json()),
            "music_level_speech_vs_gaps": windows,
            "excerpt": excerpt,
            "fal_calls": len(fal_calls),
            "provider_reservations": after_c["reservations"],
            "production_artifacts_unchanged_after_bgm": before == after_b,
            "production_artifacts_unchanged_after_bgm_change": before == after_c,
            "render_input_hashes_distinct": len(
                {meta_a["input_hash"], meta_b["input_hash"], meta_c["input_hash"]}
            )
            == 3,
        }
    )
    (out / "report.json").write_text(
        json.dumps(results, indent=1, ensure_ascii=False, sort_keys=True), encoding="utf-8"
    )
    print(json.dumps(results["speech_intelligibility"], indent=1))
    print("fal_calls", len(fal_calls), "reservations", after_c["reservations"])
    print("work dir", work, file=sys.stderr)


if __name__ == "__main__":
    main()
