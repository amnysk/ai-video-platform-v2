"""BGM の選曲（Episode 単位）と scene への配置（ADR-0083 §3 / §4、INV-134）。純粋関数。

- 選曲: ``select_approved``（ライセンスで門を閉じる。INV-97）の結果に、
  energy の一致を足して並べ直す。
  法的に通った曲だけが対象で、energy は並び順にしか効かない。同じ入力は同じ曲になる
- 配置: ``RenderPlan``（scene の時間軸と声の位置）と曲から ``MusicCueSheet`` を作る。
  scene の役割（intro / body / climax / outro / silent）で音量を変え、役割が変わる境目は
  クロスフェード。同じ曲は scene をまたいで**途切れず**続く（``source_offset_ms``）。
  silent の scene は無音（その scene には cue を置かない）
- 音量: cue の ``target_relative_level_db`` は「曲に掛ける gain」で、
  役割ごとの目標（声を基準とした LU）に、
  曲の実測ラウドネス（``BgmTrack.loudness_lufs``）と声の基準（``VOICE_REFERENCE_LUFS_CENTI``）の差を
  足して決める。曲のラウドネスが未測定の曲は配置しない（推測しない）
- ここは cue sheet を作るだけ。ライセンスは描画のたびに ``resolve_bgm`` が再検査する（INV-114）
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from datetime import datetime
from enum import StrEnum

from contracts.music import (
    LEVEL_DB_MAX,
    LEVEL_DB_MIN,
    BgmTrack,
    Energy,
    MusicCue,
    MusicCueSheet,
    Platform,
    VideoFormat,
)
from contracts.music_render import BGM_NARRATION_PRIORITY_MAX_DB, VOICE_REFERENCE_LUFS_CENTI
from contracts.render import RenderPlan
from domain.music.selection import select_approved


class SceneRole(StrEnum):
    INTRO = "intro"
    BODY = "body"
    CLIMAX = "climax"
    OUTRO = "outro"
    #: この scene には音楽を置かない（無音にしたい場面。cue が無いので BGM は鳴らない）
    SILENT = "silent"


class PlacementRefusal(StrEnum):
    TRACK_LOUDNESS_UNKNOWN = "track_loudness_unknown"
    TRACK_NOT_APPROVED = "track_not_approved"
    ALL_SCENES_SILENT = "all_scenes_silent"
    UNKNOWN_SCENE_ROLE = "unknown_scene_role"
    NO_VOICE_WINDOWS = "no_voice_windows"


@dataclass(frozen=True, slots=True)
class PlacementPolicy:
    """配置の数値。**ここが唯一の定義**。変えるときは根拠（実測）を ADR-0083 に残す。"""

    #: 役割ごとの「曲の目標ラウドネス - 声の基準」（dB, 整数）。ducking でさらに下がる前の値
    role_relative_db: Mapping[SceneRole, int] = field(
        default_factory=lambda: {
            SceneRole.INTRO: -13,
            SceneRole.BODY: -16,
            SceneRole.CLIMAX: -12,
            SceneRole.OUTRO: -14,
        }
    )
    #: 役割が変わる境目（と、無音 scene との境目）のクロスフェード長
    crossfade_ms: int = 800
    #: 動画の頭・末尾でのフェード
    opening_fade_in_ms: int = 1_500
    closing_fade_out_ms: int = 2_500


DEFAULT_POLICY = PlacementPolicy()


@dataclass(frozen=True, slots=True)
class PlacementResult:
    cue_sheet: MusicCueSheet | None
    refusal: PlacementRefusal | None = None


@dataclass(frozen=True, slots=True)
class SceneWindow:
    scene_id: str
    start_ms: int
    end_ms: int


def choose_track(
    catalog: Iterable[BgmTrack],
    *,
    mood: str,
    energy: Energy | None,
    fmt: VideoFormat,
    platform: Platform,
    duration_ms: int,
    now: datetime,
    recent_usage: Mapping[str, int],
) -> BgmTrack | None:
    """承認済みの曲から 1 曲選ぶ。無ければ ``None``（呼び出し側は BGM 無しで進める）。

    ``select_approved`` の優先順（mood・直近の使用が少ない・尺）を保ったまま、energy が合う曲を
    同順位の中で先にする。安定ソートなので、同じ入力は同じ曲を選ぶ。
    """
    result = select_approved(
        catalog,
        mood=mood,
        fmt=fmt,
        platform=platform,
        duration_ms=duration_ms,
        now=now,
        recent_usage=recent_usage,
    )
    ordered = sorted(
        enumerate(result.candidates),
        key=lambda item: (
            0 if mood in item[1].mood_tags else 1,
            recent_usage.get(item[1].track_id, 0),
            0 if energy is None or item[1].energy == energy else 1,
            item[0],
        ),
    )
    return ordered[0][1] if ordered else None


def scene_windows(plan: RenderPlan) -> tuple[SceneWindow, ...]:
    """台本 scene ごとの時間窓（その声が割り当たる storyboard scene の範囲）。声の順。"""
    by_id = {s.scene_id: s for s in plan.scenes}
    out: list[SceneWindow] = []
    for voice in plan.voices:
        scenes = [by_id[i] for i in voice.storyboard_scene_ids if i in by_id]
        if not scenes:
            continue
        out.append(
            SceneWindow(
                scene_id=voice.script_scene_id,
                start_ms=min(s.timeline_start_ms for s in scenes),
                end_ms=max(s.timeline_start_ms + s.timeline_duration_ms for s in scenes),
            )
        )
    return tuple(sorted(out, key=lambda w: (w.start_ms, w.scene_id)))


def default_roles(windows: tuple[SceneWindow, ...]) -> dict[str, SceneRole]:
    """既定の役割: 先頭 = intro、末尾 = outro、他 = body（climax・silent は呼び出し側が指定）。"""
    roles = {w.scene_id: SceneRole.BODY for w in windows}
    ordered = list(windows)
    if ordered:
        roles[ordered[0].scene_id] = SceneRole.INTRO
        roles[ordered[-1].scene_id] = SceneRole.OUTRO if len(ordered) > 1 else SceneRole.INTRO
    return roles


def gain_db_for(track: BgmTrack, role: SceneRole, policy: PlacementPolicy) -> int:
    """曲に掛ける gain（dB, 整数）。目標 = 声の基準 + 役割の相対値、gain = 目標 - 曲の実測。"""
    assert track.loudness_lufs is not None
    target_centi = VOICE_REFERENCE_LUFS_CENTI + policy.role_relative_db[role] * 100
    gain = round((target_centi - track.loudness_lufs * 100) / 100)
    return max(LEVEL_DB_MIN, min(gain, BGM_NARRATION_PRIORITY_MAX_DB, LEVEL_DB_MAX))


def plan_cue_sheet(
    plan: RenderPlan,
    track: BgmTrack,
    *,
    roles: Mapping[str, SceneRole] | None = None,
    policy: PlacementPolicy = DEFAULT_POLICY,
) -> PlacementResult:
    """scene ごとの役割から cue sheet を作る。

    連続する非 silent の scene は 1 つの「区間」で、区間の頭を曲の頭として scene ごとに cue を置く
    （``source_offset_ms`` = 区間の頭からの経過）。隣り合う cue は境目をまたいで ``crossfade_ms``
    重なり、音量の違いは線形のクロスフェードで滑らかに繋がる。
    """
    if track.license_status != "approved" or track.license_evidence_sha256 is None:
        return PlacementResult(None, PlacementRefusal.TRACK_NOT_APPROVED)
    if track.loudness_lufs is None:
        return PlacementResult(None, PlacementRefusal.TRACK_LOUDNESS_UNKNOWN)
    windows = scene_windows(plan)
    if not windows:
        return PlacementResult(None, PlacementRefusal.NO_VOICE_WINDOWS)
    assigned = {**default_roles(windows), **(roles or {})}
    known = {w.scene_id for w in windows}
    if not set(assigned) <= known:
        return PlacementResult(None, PlacementRefusal.UNKNOWN_SCENE_ROLE)

    runs: list[list[SceneWindow]] = []
    current: list[SceneWindow] = []
    for w in windows:
        if assigned[w.scene_id] is SceneRole.SILENT:
            if current:
                runs.append(current)
                current = []
        else:
            current.append(w)
    if current:
        runs.append(current)
    if not runs:
        return PlacementResult(None, PlacementRefusal.ALL_SCENES_SILENT)

    cues: list[MusicCue] = []
    total = plan.total_duration_ms
    half = policy.crossfade_ms // 2
    for run in runs:
        run_start, run_end = run[0].start_ms, run[-1].end_ms
        for i, w in enumerate(run):
            first, last = i == 0, i == len(run) - 1
            start = w.start_ms if first else max(run_start, w.start_ms - half)
            end = w.end_ms if last else min(run_end, w.end_ms + half)
            length = end - start
            fade_in = policy.opening_fade_in_ms if first and run_start == 0 else policy.crossfade_ms
            fade_out = (
                policy.closing_fade_out_ms if last and run_end >= total else policy.crossfade_ms
            )
            # 短い scene でも cue の契約（fade の合計が cue に収まる）を守る
            fade_in = min(fade_in, length // 2)
            fade_out = min(fade_out, length - fade_in)
            cues.append(
                MusicCue(
                    scene_id=w.scene_id,
                    track_id=track.track_id,
                    source_sha256=track.file_sha256,
                    start_ms=start,
                    end_ms=end,
                    source_offset_ms=start - run_start,
                    fade_in_ms=fade_in,
                    fade_out_ms=fade_out,
                    target_relative_level_db=gain_db_for(track, assigned[w.scene_id], policy),
                    ducking_policy="duck_under_voice",
                    license_evidence_hash=track.license_evidence_sha256,
                    credits_text=track.exact_attribution_text
                    if track.attribution_required
                    else None,
                )
            )
    cues.sort(key=lambda c: (c.start_ms, c.scene_id))
    return PlacementResult(MusicCueSheet(cues=tuple(cues)))


__all__ = [
    "DEFAULT_POLICY",
    "PlacementPolicy",
    "PlacementRefusal",
    "PlacementResult",
    "SceneRole",
    "SceneWindow",
    "choose_track",
    "default_roles",
    "gain_db_for",
    "plan_cue_sheet",
    "scene_windows",
]
