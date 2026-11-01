"""BGM 入り動画を本番の外で描いて「聴く」ための所有者向けツール（docs/operations/bgm-preview.md）。

本番の描画経路（``RenderActivities`` + 固定版 ffmpeg + 本物の speech-margin probe）をそのまま使い、
保存先だけを SQLite（メモリ）+ メモリ ArtifactStore + 作業ディレクトリに置き換える。
PostgreSQL・MinIO・Temporal・fal・YouTube・LLM には**つながない**（render は通信を遮断して走る）。

サブコマンド:

- ``render``        : Episode export + ローカル BGM カタログ → 試聴用 mp4 / スマホ再生 wav / 報告
- ``export``        : 本番 Episode を**読み取り専用**で export（SELECT と GET だけ）
- ``synth-catalog`` : 合成トーンの曲 1 本を載せた検証用カタログを作る（第三者の音源なし）

試聴用の出力は**すべて** ``PREVIEW_ONLY_NOT_FOR_UPLOAD__`` で始まる名前で、mp4 には metadata の
透かし（title / comment）が入る。ライセンス未承認の曲は ``--allow-unapproved`` を付けたときだけ、
**scratch にコピーしたカタログの中でだけ**承認扱いにする。本番の描画経路（``resolve_bgm``）は
未承認の曲を引き続き拒否する（このツールは本番の検査を緩めない）。
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import hashlib
import json
import logging
import shutil
import socket
import statistics
import subprocess
import sys
import tempfile
import uuid
from collections.abc import Iterator, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from sqlalchemy import select

from contracts.music import LEVEL_DB_MAX, LEVEL_DB_MIN, BgmTrack, MusicCueSheet
from contracts.render import RENDER_PROFILES
from contracts.render_activities import RenderAdmitRequest, RenderFinalVideoRequest
from contracts.states import ArtifactType
from domain.music.intelligibility import SceneSpeechLevels, evaluate_speech_intelligibility
from domain.music.placement import SceneRole, plan_cue_sheet
from domain.music.selection import reject_reasons
from domain.render.bgm import resolve_bgm
from domain.render.ports import BgmRenderInput
from infrastructure.db.models import ArtifactMetadataRow
from infrastructure.media.probe import PillowAvMediaProbe
from infrastructure.music.catalog_store import read_catalog, write_cue_sheet
from infrastructure.music.evidence import bgm_evidence_key
from infrastructure.render.bgm_intelligibility import FfmpegSpeechMarginProbe
from infrastructure.render.bgm_source import CATALOG_FILE_NAME, LocalDirectoryBgmSource
from infrastructure.render.binary import file_sha256
from infrastructure.render.ffmpeg_engine import FfmpegRenderEngine
from infrastructure.render.loudness import FfmpegLoudnessProbe
from infrastructure.storage.memory_store import InMemoryArtifactStore
from infrastructure.workdir import MINIO_DATA_DIR, WorkDirectory
from tests.support.bgm_preview_db import memory_session_factory
from tests.support.bgm_verify_export import (
    BUCKET,
    DEFAULT_FFMPEG,
    FFMPEG_SHA256,
    FONT,
    ebur128,
    probe_streams,
    seed,
    speech_and_gap_levels,
)

PREVIEW_MARK = "PREVIEW ONLY - NOT FOR UPLOAD"
FILE_PREFIX = "PREVIEW_ONLY_NOT_FOR_UPLOAD"
#: 音声・映像の尺の差がこれ以下なら同期 OK とみなす（ms）
SYNC_TOLERANCE_MS = 40
#: スマホのスピーカー相当（ざっくり）: 300 Hz 未満を切り、7 kHz 超を落とし、モノラルにする
PHONE_FILTER = "highpass=f=300,lowpass=f=7000,pan=mono|c0=0.5*c0+0.5*c1"
PREVIEW_PROFILE_DEFAULT = "shorts_vertical"
SYNTH_EVIDENCE = b"self-generated synthetic tone (tests/support/bgm_synth.py); no third-party audio"
#: 未承認の曲を試聴するときに scratch の複製へ入れる証拠の代わり（本物の証拠ではない）
PREVIEW_EVIDENCE = f"{PREVIEW_MARK}: not a license evidence. Audition copy only.".encode()
#: 読み込んではならない（fal / production worker）。1 つでも読まれていたら失敗
FORBIDDEN_MODULE_PREFIXES = (
    "infrastructure.providers",
    "workers.production",
    "workers.generation",
)


class PreviewError(Exception):
    """入力不備・安全装置の発動。終了コード 2 で止める（描画は始めない）。"""


# ----------------------------------------------------------------------------- 通信・fal の遮断
@dataclass
class OfflineState:
    blocked_connects: list[str] = field(default_factory=list)


@contextlib.contextmanager
def offline_guard() -> Iterator[OfflineState]:
    """このプロセスのネットワーク接続を遮断する（UNIX ソケットだけ許す。asyncio の内部用）。

    ffmpeg は子プロセスで、入力は全てローカルファイル。DNS 解決も禁止する。
    """
    state = OfflineState()
    real_connect = socket.socket.connect
    real_getaddrinfo = socket.getaddrinfo

    def connect(self: socket.socket, address: Any) -> Any:
        if self.family == socket.AF_UNIX:
            return real_connect(self, address)
        state.blocked_connects.append(repr(address))
        raise PreviewError(f"network connect is blocked in bgm-preview: {address!r}")

    def getaddrinfo(*args: Any, **kwargs: Any) -> Any:
        state.blocked_connects.append(f"dns:{args[0]!r}")
        raise PreviewError(f"name resolution is blocked in bgm-preview: {args[0]!r}")

    socket.socket.connect = connect  # type: ignore[method-assign]
    socket.getaddrinfo = getaddrinfo  # type: ignore[assignment]
    try:
        yield state
    finally:
        socket.socket.connect = real_connect  # type: ignore[method-assign]
        socket.getaddrinfo = real_getaddrinfo


def loaded_forbidden_modules() -> list[str]:
    """fal クライアント等の課金経路が読み込まれていないことの証拠（読まれていれば構築されうる）。"""
    return sorted(m for m in sys.modules if m.startswith(FORBIDDEN_MODULE_PREFIXES))


def assert_no_paid_path_loaded() -> None:
    loaded = loaded_forbidden_modules()
    if loaded:
        raise PreviewError(f"paid/production modules were loaded: {loaded}")


# ------------------------------------------------------------ カタログ（scratch の複製）
@dataclass(frozen=True)
class PreparedTrack:
    track: BgmTrack  # scratch の複製に書いた（試聴用に承認扱いにした場合がある）
    original_status: str
    production_reject_reasons: tuple[str, ...]
    overridden_fields: tuple[str, ...]
    loudness_measured: bool


def preview_override(
    track: BgmTrack, *, now: datetime, profile_format: str
) -> tuple[BgmTrack, tuple[str, ...]]:
    """本番が拒否する曲を、**試聴の複製の中でだけ**通る形にする。変えた項目名を返す。

    本番のカタログ・``resolve_bgm`` には触れない。変えた項目は report に全て書く。
    """
    changes: dict[str, Any] = {}
    if track.license_status != "approved":
        changes["license_status"] = "approved"
    if track.license_status != "approved" or track.license_evidence_sha256 is None:
        changes["license_evidence_sha256"] = hashlib.sha256(PREVIEW_EVIDENCE).hexdigest()
    if track.license_checked_at is None or track.license_checked_at > now:
        changes["license_checked_at"] = now - timedelta(days=1)
    if track.status != "active":
        changes["status"] = "active"
    if track.valid_until is not None and track.valid_until <= now:
        changes["valid_until"] = None
    if "youtube" not in track.allowed_platforms:
        changes["allowed_platforms"] = (*track.allowed_platforms, "youtube")
    if profile_format not in track.allowed_formats:
        changes["allowed_formats"] = (*track.allowed_formats, profile_format)
    for flag in ("commercial_use_allowed", "reuse_allowed", "modification_allowed"):
        if not getattr(track, flag):
            changes[flag] = True
    if not changes:
        return track, ()
    return track.model_copy(update=changes), tuple(sorted(changes))


def prepare_catalog(
    catalog_dir: Path,
    track_ids: Sequence[str],
    scratch: Path,
    *,
    allow_unapproved: bool,
    ffmpeg: Path,
    now: datetime,
    profile_format: str = "shorts",
) -> dict[str, PreparedTrack]:
    """``catalog_dir`` を**読むだけ**で、scratch に試聴用の複製カタログを作る。"""
    by_id = {t.track_id: t for t in read_catalog(catalog_dir)}
    missing = [t for t in track_ids if t not in by_id]
    if missing:
        raise PreviewError(f"track not in catalog: {missing} (catalog has {sorted(by_id)})")
    prepared: dict[str, PreparedTrack] = {}
    for tid in track_ids:
        track = by_id[tid]
        source = catalog_dir / track.object_key
        if not source.is_file():
            raise PreviewError(f"audio file missing: {source}")
        if file_sha256(source) != track.file_sha256:
            raise PreviewError(f"audio file sha256 != catalog file_sha256 for {tid}")
        reasons = reject_reasons(track, now=now, fmt=profile_format, platform="youtube")  # type: ignore[arg-type]
        overridden: tuple[str, ...] = ()
        if reasons:
            if not allow_unapproved:
                raise PreviewError(
                    f"track {tid} would be rejected by the production path "
                    f"({', '.join(r.value for r in reasons)}). "
                    "Pass --allow-unapproved to audition it in this preview tool only."
                )
            track, overridden = preview_override(track, now=now, profile_format=profile_format)
        measured = False
        if track.loudness_lufs is None:
            loud = FfmpegLoudnessProbe(ffmpeg).measure_loudness(str(source))
            if loud.integrated_lufs_centi is None:
                raise PreviewError(f"could not measure loudness of {tid}")
            track = track.model_copy(update={"loudness_lufs": loud.integrated_lufs_centi / 100})
            measured = True
        target = scratch / track.object_key
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(source, target)  # 複製（symlink は LocalDirectoryBgmSource が拒否する）
        prepared[tid] = PreparedTrack(
            track=track,
            original_status=by_id[tid].license_status,
            production_reject_reasons=tuple(r.value for r in reasons),
            overridden_fields=overridden,
            loudness_measured=measured,
        )
    payload = {"tracks": [p.track.model_dump(mode="json") for p in prepared.values()]}
    (scratch / CATALOG_FILE_NAME).write_text(
        json.dumps(payload, ensure_ascii=False, sort_keys=True, indent=1) + "\n", encoding="utf-8"
    )
    return prepared


# ----------------------------------------------------------------------------- cue sheet / 数値
def parse_offsets(text: str) -> tuple[int, ...]:
    try:
        values = tuple(int(x) for x in text.split(",") if x.strip() != "")
    except ValueError as exc:
        raise PreviewError(
            f"--level-offsets must be comma separated integers (dB): {text!r}"
        ) from exc
    if not values:
        raise PreviewError("--level-offsets is empty")
    return tuple(dict.fromkeys(values))  # 順序を保って重複を除く


def parse_roles(text: str | None) -> dict[str, SceneRole]:
    roles: dict[str, SceneRole] = {}
    for part in (text or "").split(","):
        if not part.strip():
            continue
        scene, _, role = part.partition("=")
        try:
            roles[scene.strip()] = SceneRole(role.strip())
        except ValueError as exc:
            raise PreviewError(
                f"unknown role in --roles: {part!r} (allowed: {[r.value for r in SceneRole]})"
            ) from exc
    return roles


def shift_levels(sheet: MusicCueSheet, offset_db: int) -> MusicCueSheet:
    """全 cue の ``target_relative_level_db`` を offset だけずらす（契約の範囲に丸める）。"""
    return MusicCueSheet(
        cues=tuple(
            c.model_copy(
                update={
                    "target_relative_level_db": min(
                        LEVEL_DB_MAX, max(LEVEL_DB_MIN, c.target_relative_level_db + offset_db)
                    )
                }
            )
            for c in sheet.cues
        )
    )


def summarize_margins(scenes: Sequence[Any]) -> dict[str, Any]:
    margins = [s.margin_centi_lu / 100 for s in scenes if s.margin_centi_lu is not None]
    return {
        "min_margin_lu": min(margins) if margins else None,
        "median_margin_lu": statistics.median(margins) if margins else None,
        "per_scene": [
            {
                "scene_id": s.scene_id,
                "voice_lufs": None if s.voice_lufs_centi is None else s.voice_lufs_centi / 100,
                "bgm_lufs": None if s.music_lufs_centi is None else s.music_lufs_centi / 100,
                "margin_lu": None if s.margin_centi_lu is None else s.margin_centi_lu / 100,
            }
            for s in scenes
        ],
    }


def summarize_gaps(windows: dict[str, Any]) -> dict[str, Any]:
    gap = [w["music_lufs"] for w in windows["gap_windows"] if w["music_lufs"] is not None]
    speech = [w["music_lufs"] for w in windows["speech_windows"] if w["music_lufs"] is not None]
    return {
        "bgm_in_gaps_lufs": {
            "windows": len(windows["gap_windows"]),
            "mean": round(statistics.mean(gap), 2) if gap else None,
            "min": min(gap) if gap else None,
            "max": max(gap) if gap else None,
        },
        "bgm_in_speech_lufs_mean": round(statistics.mean(speech), 2) if speech else None,
        "gap_minus_speech_lu": (
            round(statistics.mean(gap) - statistics.mean(speech), 2) if gap and speech else None
        ),
    }


def sync_check(streams: dict[str, Any], plan_total_ms: int) -> dict[str, Any]:
    video = streams.get("video", {})
    audio = streams.get("audio", {})
    v_ms = round(video.get("duration_s", 0) * 1000)
    a_ms = round(audio.get("duration_s", 0) * 1000)
    fmt_ms = round(streams["format_duration_s"] * 1000)
    start_delta = round((audio.get("start_time_s", 0.0) - video.get("start_time_s", 0.0)) * 1000)
    ok = (
        abs(a_ms - v_ms) <= SYNC_TOLERANCE_MS
        and abs(fmt_ms - plan_total_ms) <= SYNC_TOLERANCE_MS
        and abs(start_delta) <= SYNC_TOLERANCE_MS
    )
    return {
        "plan_total_ms": plan_total_ms,
        "format_ms": fmt_ms,
        "video_ms": v_ms,
        "audio_ms": a_ms,
        "audio_minus_video_start_ms": start_delta,
        "tolerance_ms": SYNC_TOLERANCE_MS,
        "ok": ok,
    }


# ----------------------------------------------------------------------------- ffmpeg 補助
def _ff(ffmpeg: Path, *args: str) -> None:
    done = subprocess.run(  # noqa: S603 - 固定版 ffmpeg、argv は固定
        [str(ffmpeg), "-hide_banner", "-nostdin", "-nostats", "-loglevel", "error", "-y", *args],
        capture_output=True,
        text=True,
        check=False,
    )
    if done.returncode != 0:
        raise PreviewError(f"ffmpeg failed: {done.stderr[-400:]}")


def watermark_remux(ffmpeg: Path, src: Path, dst: Path, note: str) -> None:
    """映像・音声は再エンコードせず、metadata に透かしを入れる（title / comment）。"""
    _ff(
        ffmpeg, "-i", str(src), "-map", "0", "-c", "copy",
        "-metadata", f"title={PREVIEW_MARK}", "-metadata", f"comment={PREVIEW_MARK}; {note}",
        str(dst),
    )  # fmt: skip


def phone_simulation(ffmpeg: Path, src: Path, dst: Path) -> None:
    _ff(
        ffmpeg,
        "-i",
        str(src),
        "-vn",
        "-af",
        PHONE_FILTER,
        "-ar",
        "48000",
        "-c:a",
        "pcm_s16le",
        str(dst),
    )


# ----------------------------------------------------------------------------- 描画
@dataclass
class PreviewOptions:
    export_dir: Path
    catalog_dir: Path
    track_ids: tuple[str, ...]
    out_dir: Path
    ffmpeg: Path = DEFAULT_FFMPEG
    offsets: tuple[int, ...] = (0,)
    profile_id: str = PREVIEW_PROFILE_DEFAULT
    roles: dict[str, SceneRole] = field(default_factory=dict)
    allow_unapproved: bool = False


def check_out_dir(out: Path, export: Path) -> None:
    resolved = out.resolve()
    for forbidden in (MINIO_DATA_DIR, export.resolve()):
        if resolved == forbidden or forbidden in resolved.parents or resolved in forbidden.parents:
            raise PreviewError(f"output dir {out} overlaps {forbidden} (never write there)")


def _report_markdown(report: dict[str, Any]) -> str:
    sp = report.get("speech_intelligibility", {})
    lines = [
        f"# {PREVIEW_MARK}",
        "",
        f"- variant: `{report['variant']}`",
        f"- file: `{report['file']}`",
        f"- phone simulation (listen on speakers / headphones low): `{report['phone_file']}`",
        f"- integrated loudness: {report['loudness']['integrated_lufs']} LUFS / "
        f"true peak {report['loudness']['true_peak_dbfs']} dBFS / "
        f"LRA {report['loudness']['lra_lu']} LU",
        f"- sync: {'OK' if report['sync']['ok'] else 'NG'} {json.dumps(report['sync'])}",
    ]
    if sp:
        lines += [
            f"- voice - BGM margin during speech: min {sp['min_margin_lu']} LU / "
            f"median {sp['median_margin_lu']} LU (threshold {sp['threshold_lu']} LU, "
            f"{'PASS' if sp['passed'] else 'FAIL'})",
            f"- BGM in non-speech gaps: {json.dumps(report['gaps'])}",
            f"- mix cue levels (dB re voice): {report['mix_levels_db']} "
            f"(offset {report['offset_db']:+d} dB)",
        ]
    return "\n".join(lines) + "\n"


async def run_preview(opts: PreviewOptions) -> dict[str, Any]:
    now = datetime.now(UTC)
    if opts.profile_id not in RENDER_PROFILES:
        raise PreviewError(
            f"unknown profile {opts.profile_id!r} (known: {sorted(RENDER_PROFILES)})"
        )
    if not opts.ffmpeg.is_file():
        raise PreviewError(f"ffmpeg not found: {opts.ffmpeg}")
    check_out_dir(opts.out_dir, opts.export_dir)
    opts.out_dir.mkdir(parents=True, exist_ok=True)
    scratch = Path(tempfile.mkdtemp(prefix="bgm-preview-"))
    bgm_root = scratch / "bgm"
    profile_format = "shorts" if "shorts" in opts.profile_id else "longform"
    prepared = prepare_catalog(
        opts.catalog_dir, opts.track_ids, bgm_root, allow_unapproved=opts.allow_unapproved,
        ffmpeg=opts.ffmpeg, now=now, profile_format=profile_format,
    )  # fmt: skip

    factory = await memory_session_factory()
    store = InMemoryArtifactStore()
    render_engine = FfmpegRenderEngine.from_binary(opts.ffmpeg, FFMPEG_SHA256)
    media_probe = PillowAvMediaProbe()
    loud_probe = FfmpegLoudnessProbe(opts.ffmpeg)
    speech_probe = FfmpegSpeechMarginProbe(opts.ffmpeg)
    source = LocalDirectoryBgmSource(bgm_root)

    def activities(with_bgm: bool) -> Any:
        from workers.render.activities import RenderActivities

        return RenderActivities(
            session_factory=factory, store=store, bucket=BUCKET,
            workdir=WorkDirectory(scratch / "render-work"),
            engine=render_engine, probe=media_probe, source_probe=media_probe,
            render_threads=4, font_path=FONT, font_sha256=file_sha256(FONT),
            render_timeout_seconds=900, min_free_bytes=1_000,
            bgm_source=source if with_bgm else None,
            loudness_probe=loud_probe if with_bgm else None,
            speech_probe=speech_probe if with_bgm else None,
        )  # fmt: skip

    with offline_guard() as net:
        episode_id, voice_wavs = await seed(factory, store, opts.export_dir)
        ep8 = episode_id[:8]
        request = RenderFinalVideoRequest(episode_id, "bgm-preview", "run-1", opts.profile_id)

        async def current_final() -> tuple[Any, dict[str, Any]]:
            async with factory() as session:
                row = (
                    await session.scalars(
                        select(ArtifactMetadataRow).where(
                            ArtifactMetadataRow.artifact_type == ArtifactType.FINAL_VIDEO.value,
                            ArtifactMetadataRow.superseded_at.is_(None),
                        )
                    )
                ).one()
            return row, await store.get_json(row.object_key)

        async def save_current(dst_raw: Path) -> tuple[str, str]:
            row, payload = await current_final()
            dst_raw.write_bytes(await store.get_bytes(payload["media"]["object_key"]))
            return row.input_hash, payload["media"]["sha256"]

        def measure(raw: Path, final: Path, phone: Path, note: str, plan_ms: int) -> dict[str, Any]:
            watermark_remux(opts.ffmpeg, raw, final, note)
            phone_simulation(opts.ffmpeg, final, phone)
            streams = probe_streams(opts.ffmpeg, final)
            return {
                "file": final.name,
                "phone_file": phone.name,
                "preview_mark": PREVIEW_MARK,
                "loudness": ebur128(opts.ffmpeg, final),
                "streams": streams,
                "sync": sync_check(streams, plan_ms),
            }

        # A: BGM 無し（本番の BGM OFF と同じ経路）
        raw_dir = scratch / "raw"
        raw_dir.mkdir()
        act_off = activities(False)
        await act_off.admit(RenderAdmitRequest(episode_id, "bgm-preview", "run-1"))
        await act_off.render_final_video(request)
        hash_off, _ = await save_current(raw_dir / "without_bgm.mp4")

        act_on = activities(True)
        plan = await _plan(act_on, episode_id, render_engine, opts.profile_id)
        base_name = f"{FILE_PREFIX}__{ep8}"
        off_report = measure(
            raw_dir / "without_bgm.mp4",
            opts.out_dir / f"{base_name}__without_bgm.mp4",
            opts.out_dir / f"{base_name}__without_bgm__phone.wav",
            "without BGM (A of A/B)",
            plan.total_duration_ms,
        )
        off_report["variant"] = "without_bgm"
        (opts.out_dir / f"{base_name}__without_bgm__report.json").write_text(
            json.dumps(off_report, indent=1, ensure_ascii=False, sort_keys=True), encoding="utf-8"
        )

        variants: list[dict[str, Any]] = []
        for tid, prep in prepared.items():
            placed = plan_cue_sheet(plan, prep.track, roles=opts.roles or None)
            if placed.cue_sheet is None:
                raise PreviewError(f"cue placement refused for {tid}: {placed.refusal}")
            for offset in opts.offsets:
                sheet = shift_levels(placed.cue_sheet, offset)
                write_cue_sheet(bgm_root, episode_id, sheet)
                resolved = resolve_bgm(
                    sheet, [prep.track], plan=plan, now=datetime.now(UTC),
                    file_sha256_by_track={tid: prep.track.file_sha256},
                )  # fmt: skip
                if not resolved.applied or resolved.plan is None:
                    raise PreviewError(
                        f"production resolve_bgm rejected the preview copy of {tid}: "
                        f"{[(r.reason.value, r.detail) for r in resolved.rejections]}"
                    )
                tag = f"offset_{offset:+d}dB"
                vdir = opts.out_dir / tid / tag
                vdir.mkdir(parents=True, exist_ok=True)
                await act_on.render_final_video(request)
                input_hash, media_sha = await save_current(raw_dir / f"{tid}_{tag}.mp4")
                if input_hash == hash_off or not await store.exists(
                    bgm_evidence_key(episode_id, media_sha, input_hash)
                ):
                    raise PreviewError(f"BGM was not applied by the render for {tid} {tag}")
                name = f"{base_name}__{tid}__{tag}"
                report = measure(
                    raw_dir / f"{tid}_{tag}.mp4",
                    vdir / f"{name}.mp4",
                    vdir / f"{name}__phone.wav",
                    f"track={tid} offset={offset:+d}dB catalog_status={prep.original_status}",
                    plan.total_duration_ms,
                )
                bgm_input = BgmRenderInput(
                    mix=resolved.plan, track_paths={tid: bgm_root / prep.track.object_key}
                )
                voice_wav, music_wav = speech_probe.write_stems(
                    plan, bgm_input, voice_wavs, vdir / "stems"
                )
                levels = tuple(
                    SceneSpeechLevels(
                        v.script_scene_id,
                        speech_probe.window_lufs(voice_wav, v.start_ms, v.duration_ms),
                        speech_probe.window_lufs(music_wav, v.start_ms, v.duration_ms),
                    )
                    for v in plan.voices
                )
                verdict = evaluate_speech_intelligibility(levels)
                margins = summarize_margins(verdict.scenes)
                report.update(
                    {
                        "variant": f"{tid}/{tag}",
                        "track_id": tid,
                        "offset_db": offset,
                        "input_hash": input_hash,
                        "render_input_hash_differs_from_without_bgm": input_hash != hash_off,
                        "mix_levels_db": [c.level_db for c in resolved.plan.cues],
                        "license": {
                            "catalog_status": prep.original_status,
                            "production_would_reject": list(prep.production_reject_reasons),
                            "overridden_for_preview_only": list(prep.overridden_fields),
                            "loudness_measured_for_preview": prep.loudness_measured,
                        },
                        "speech_intelligibility": {
                            **margins,
                            "threshold_lu": verdict.threshold_centi_lu / 100,
                            "passed": verdict.passed,
                        },
                        "gaps": summarize_gaps(
                            speech_and_gap_levels(speech_probe, plan, music_wav)
                        ),
                        "ab_without_bgm": off_report["file"],
                    }
                )
                (vdir / "report.json").write_text(
                    json.dumps(report, indent=1, ensure_ascii=False, sort_keys=True),
                    encoding="utf-8",
                )
                (vdir / "report.md").write_text(_report_markdown(report), encoding="utf-8")
                variants.append(report)
        blocked = list(net.blocked_connects)

    assert_no_paid_path_loaded()
    summary = {
        "preview_mark": PREVIEW_MARK,
        "episode_id": episode_id,
        "profile": opts.profile_id,
        "ffmpeg": str(opts.ffmpeg),
        "without_bgm": off_report,
        "variants": [
            {
                "variant": v["variant"],
                "file": v["file"],
                "integrated_lufs": v["loudness"]["integrated_lufs"],
                "true_peak_dbfs": v["loudness"]["true_peak_dbfs"],
                "min_margin_lu": v["speech_intelligibility"]["min_margin_lu"],
                "median_margin_lu": v["speech_intelligibility"]["median_margin_lu"],
                "margin_passed": v["speech_intelligibility"]["passed"],
                "gap_bgm_mean_lufs": v["gaps"]["bgm_in_gaps_lufs"]["mean"],
                "sync_ok": v["sync"]["ok"],
                "license_overridden": v["license"]["overridden_for_preview_only"],
            }
            for v in variants
        ],
        "safety": {
            "fal_or_production_modules_loaded": loaded_forbidden_modules(),
            "blocked_network_attempts": blocked,
            "production_defaults_changed": False,
        },
    }
    (opts.out_dir / "summary.json").write_text(
        json.dumps(summary, indent=1, ensure_ascii=False, sort_keys=True), encoding="utf-8"
    )
    (opts.out_dir / f"{FILE_PREFIX}__README.txt").write_text(
        f"{PREVIEW_MARK}\nAll files here are audition copies. Do not upload.\n"
        "See summary.json and <track>/<offset>/report.md.\n",
        encoding="utf-8",
    )
    shutil.rmtree(scratch, ignore_errors=True)
    return summary


async def _plan(act: Any, episode_id: str, engine: FfmpegRenderEngine, profile_id: str) -> Any:
    from domain.render.plan import build_render_plan

    inputs = await act._load_inputs(episode_id)  # noqa: SLF001 - 本番と同じ入力の読み方
    return build_render_plan(
        manifest=inputs.manifest, script=inputs.script, storyboard=inputs.storyboard,
        voices=inputs.voices, videos=inputs.videos, profile=RENDER_PROFILES[profile_id],
        policy=act._policy, engine=engine.identity(),  # noqa: SLF001
    )  # fmt: skip


# ------------------------------------------------------------ export（読み取り専用）
EXPORT_TYPES = (
    ArtifactType.SCRIPT,
    ArtifactType.STORYBOARD,
    ArtifactType.PRODUCTION_MANIFEST,
    ArtifactType.SCENE_VIDEO,
    ArtifactType.SCENE_VOICE,
)


async def export_episode(factory: Any, store: Any, episode_id: str, out: Path) -> dict[str, Any]:
    """現行 artifact の JSON と media を ``out`` へ書く。SELECT と GET だけ。"""
    from sqlalchemy import text

    out.mkdir(parents=True, exist_ok=True)
    written: dict[str, int] = {}
    async with factory() as session:
        with contextlib.suppress(
            Exception
        ):  # sqlite（テスト）には無い。PostgreSQL では読み取り専用にする
            await session.execute(text("SET TRANSACTION READ ONLY"))
        rows = (
            await session.scalars(
                select(ArtifactMetadataRow).where(
                    ArtifactMetadataRow.episode_id == uuid.UUID(episode_id),
                    ArtifactMetadataRow.superseded_at.is_(None),
                    ArtifactMetadataRow.artifact_type.in_([t.value for t in EXPORT_TYPES]),
                )
            )
        ).all()
    for row in rows:
        payload = await store.get_json(row.object_key)
        parts = [row.artifact_type] + ([row.scene_id] if row.scene_id else [])
        target = out.joinpath(*parts, f"{row.sha256}.json")
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
        written[row.artifact_type] = written.get(row.artifact_type, 0) + 1
        media = payload.get("media")
        if media and row.artifact_type in {
            ArtifactType.SCENE_VIDEO.value,
            ArtifactType.SCENE_VOICE.value,
        }:
            media_path = out / media["object_key"]
            media_path.parent.mkdir(parents=True, exist_ok=True)
            media_path.write_bytes(await store.get_bytes(media["object_key"]))
    return {"episode_id": episode_id, "out": str(out), "artifacts": written}


# ----------------------------------------------------------------------------- 合成カタログ
def make_synthetic_catalog(
    root: Path, *, seconds: int = 32, license_status: str = "unverified"
) -> str:
    """検証用: 合成の曲 1 本を載せたカタログを作る。第三者の音源なし。track_id を返す。"""
    from tests.support.bgm_synth import synthesize

    audio = synthesize(seconds)
    sha = hashlib.sha256(audio).hexdigest()
    track = BgmTrack(
        track_id="bgm-synthetic-preview",
        file_sha256=sha,
        object_key=f"music/originals/{sha}.wav",
        title="Synthetic preview tone",
        artist="avp2 test synthesizer",
        source_url="https://example.invalid/self-generated",
        source_kind="original",
        license_type="custom",
        license_status=license_status,  # type: ignore[arg-type]
        license_evidence_sha256=(
            hashlib.sha256(SYNTH_EVIDENCE).hexdigest() if license_status == "approved" else None
        ),
        license_checked_at=(
            datetime.now(UTC) - timedelta(days=1) if license_status == "approved" else None
        ),
        allowed_platforms=("youtube",),
        allowed_formats=("shorts", "longform"),
        commercial_use_allowed=True,
        reuse_allowed=True,
        modification_allowed=True,
        attribution_required=False,
        energy="low",
        duration_ms=seconds * 1000,
        status="active",
    )
    target = root / track.object_key
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(audio)
    (root / CATALOG_FILE_NAME).write_text(
        json.dumps({"tracks": [track.model_dump(mode="json")]}, indent=1, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    return track.track_id


# ----------------------------------------------------------------------------- CLI
def build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(
        prog="bgm_preview", description="BGM 入り動画を本番の外で描いて試聴する（PREVIEW ONLY）"
    )
    sub = ap.add_subparsers(dest="command", required=True)
    r = sub.add_parser(
        "render", help="export + カタログから試聴用の動画・レポートを作る（通信なし）"
    )
    r.add_argument("export_dir", type=Path)
    r.add_argument("--catalog", type=Path, required=True, help="catalog.json と music/originals/")
    r.add_argument("--track", action="append", required=True, dest="tracks", metavar="TRACK_ID")
    r.add_argument("--out", type=Path, required=True)
    r.add_argument("--ffmpeg", type=Path, default=DEFAULT_FFMPEG)
    r.add_argument("--profile", default=PREVIEW_PROFILE_DEFAULT)
    r.add_argument(
        "--level-offsets", default="0", help="cue 音量のずらし(dB)。例 -3,0,3（報告のみ）"
    )
    r.add_argument("--roles", default=None, help="例 s1=intro,s3=silent,s4=climax")
    r.add_argument("--allow-unapproved", action="store_true", help="未承認の曲を試聴専用に通す")
    e = sub.add_parser(
        "export", help="本番 Episode を読み取り専用で export する（SELECT/GET のみ）"
    )
    e.add_argument("episode_id")
    e.add_argument("out", type=Path)
    s = sub.add_parser("synth-catalog", help="合成トーンの検証用カタログを作る")
    s.add_argument("catalog_dir", type=Path)
    s.add_argument("--seconds", type=int, default=32)
    s.add_argument("--approved", action="store_true", help="既定は license_status=unverified")
    return ap


def options_from_args(args: argparse.Namespace) -> PreviewOptions:
    return PreviewOptions(
        export_dir=args.export_dir.resolve(),
        catalog_dir=args.catalog.resolve(),
        track_ids=tuple(dict.fromkeys(args.tracks)),
        out_dir=args.out.resolve(),
        ffmpeg=args.ffmpeg,
        offsets=parse_offsets(args.level_offsets),
        profile_id=args.profile,
        roles=parse_roles(args.roles),
        allow_unapproved=args.allow_unapproved,
    )


async def _export_main(args: argparse.Namespace) -> dict[str, Any]:
    from infrastructure.config import Settings
    from infrastructure.db.session import session_factory_from_settings
    from infrastructure.storage.minio_store import MinioArtifactStore

    settings = Settings()
    return await export_episode(
        session_factory_from_settings(settings),
        MinioArtifactStore.from_settings(settings),
        args.episode_id,
        args.out.resolve(),
    )


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    logging.basicConfig(level=logging.WARNING, format="%(name)s: %(message)s")
    try:
        if args.command == "render":
            summary = asyncio.run(run_preview(options_from_args(args)))
            print(json.dumps(summary["variants"], indent=1, ensure_ascii=False))
            print(f"{PREVIEW_MARK}: outputs in {args.out.resolve()}", file=sys.stderr)
        elif args.command == "export":
            print(json.dumps(asyncio.run(_export_main(args)), ensure_ascii=False))
        else:
            tid = make_synthetic_catalog(
                args.catalog_dir.resolve(),
                seconds=args.seconds,
                license_status="approved" if args.approved else "unverified",
            )
            print(tid)
    except PreviewError as exc:
        print(f"bgm-preview: {exc}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
