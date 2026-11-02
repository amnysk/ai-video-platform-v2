"""フォールバックの梯子（ADR-0087 §6 / §7、INV-154 / INV-155）。Render の内側で 1 本だけ。

選んだ曲で発話中の差を**描画の前に**音声 stem で実測し、足りなければ

1. 同じ曲の gain を ``FALLBACK_GAIN_STEP_DB`` 下げて 1 回だけ再測定
2. まだ足りない・音源が読めない・ライセンスの再検査に落ちた・配置できない -> 次点の曲（合計
   ``FALLBACK_MAX_TRACKS`` 曲まで）
3. それでも通らなければ BGM 無し

ループは有限（曲数 x 2 回の測定が上限）。fal・production・ネットワークには触れない。結果は
``BgmSelectionDecision``（適用 gain・実測・経過）で、保存は呼び出し側が ``finalize`` で行う。
"""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass

from contracts.music import MusicCueSheet
from contracts.music_render import SpeechIntelligibility
from contracts.music_selection import (
    FALLBACK_GAIN_STEP_DB,
    FALLBACK_MAX_GAIN_STEPS,
    BgmSelectionDecision,
    FallbackEvent,
    FallbackReason,
    FallbackStep,
)
from domain.artifact.hashing import canonical_json_bytes, sha256_hex
from domain.render.bgm import BgmResolution, BgmSkipReason
from domain.render.ports import (
    BgmCandidate,
    BgmRenderContext,
    BgmRenderInput,
    BgmRenderSelection,
    BgmSourceData,
    SelectingBgmSource,
)

logger = logging.getLogger(__name__)

#: 測定: ``None`` は測れなかった（推測で通さない）
Measure = Callable[[BgmRenderInput], Awaitable[SpeechIntelligibility | None]]
Resolve = Callable[[BgmSourceData], BgmResolution]


@dataclass(frozen=True, slots=True)
class LadderResult:
    decision: BgmSelectionDecision
    #: BGM を使うときだけ。``None`` なら BGM 無し
    resolution: BgmResolution | None
    data: BgmSourceData | None
    speech: SpeechIntelligibility | None


def cue_sheet_sha256(sheet: MusicCueSheet) -> str:
    return sha256_hex(canonical_json_bytes(sheet.model_dump(mode="json")))


def reason_for_skip(resolution: BgmResolution) -> FallbackReason:
    """``resolve_bgm`` の拒否理由を梯子の理由コードへ畳む。"""
    reasons = {r.reason for r in resolution.rejections}
    if BgmSkipReason.LICENSE_REJECTED in reasons or BgmSkipReason.CREDITS_INVALID in reasons:
        return FallbackReason.LICENSE_RECHECK_FAILED
    if reasons & {BgmSkipReason.FILE_MISSING_OR_ALTERED, BgmSkipReason.SOURCE_UNREADABLE}:
        return FallbackReason.SOURCE_UNREADABLE
    return FallbackReason.PLACEMENT_REFUSED


def log_selection(
    decision: BgmSelectionDecision, *, brand_id: str, finalized: bool, reused: bool
) -> None:
    """``bgm.selection``（secret・台本の原文を含めない。固定語彙と数値だけ）。"""
    top = decision.ranked[0].total if decision.ranked else None
    logger.info(
        "bgm.selection episode_id=%s brand_id=%s track_id=%s score=%s gain=%s margin=%s "
        "finalized=%s reused=%s rule_version=%s terms=%s reasons=%s",
        decision.episode_id,
        brand_id,
        decision.track_id,
        top,
        decision.applied_gain_offset_db,
        decision.min_speech_margin_centi_lu,
        finalized,
        reused,
        decision.rule_version,
        list(decision.requirements.matched_terms),
        [e.reason.value for e in decision.fallback if e.reason is not None],
    )


def log_fallback(episode_id: str, brand_id: str, event: FallbackEvent) -> None:
    """``bgm.fallback``。"""
    logger.warning(
        "bgm.fallback episode_id=%s brand_id=%s step=%s track_id=%s reason=%s gain=%s margin=%s",
        episode_id,
        brand_id,
        event.step.value,
        event.track_id,
        event.reason.value if event.reason else None,
        event.gain_offset_db,
        event.min_speech_margin_centi_lu,
    )


async def run_ladder(
    source: SelectingBgmSource,
    ctx: BgmRenderContext,
    selection: BgmRenderSelection,
    *,
    resolve: Resolve,
    measure: Measure | None,
    heartbeat: Callable[[], object] = lambda: None,
) -> LadderResult:
    """未確定の選曲に梯子を適用する。``measure`` が ``None`` なら実測せず最初の曲を使う。"""
    base = selection.decision
    brand_id = base.requirements.brand_id
    events: list[FallbackEvent] = list(base.fallback)

    def note(event: FallbackEvent) -> None:
        events.append(event)
        log_fallback(base.episode_id, brand_id, event)

    last_reason = FallbackReason.NO_APPROVED_TRACK
    for track_id in selection.candidates:
        extra = 0
        previous_plan = None
        for _ in range(FALLBACK_MAX_GAIN_STEPS + 1):
            heartbeat()
            cand: BgmCandidate = await source.prepare_candidate(
                ctx, track_id, extra_gain_offset_db=extra
            )
            if cand.failure is not None or cand.data is None:
                last_reason = cand.failure or FallbackReason.SOURCE_UNREADABLE
                note(
                    FallbackEvent(
                        step=FallbackStep.NEXT_TRACK,
                        track_id=track_id,
                        reason=last_reason,
                        gain_offset_db=extra,
                    )
                )
                break
            resolution = resolve(cand.data)
            if not resolution.applied or resolution.plan is None or cand.data.cue_sheet is None:
                last_reason = reason_for_skip(resolution)
                note(
                    FallbackEvent(
                        step=FallbackStep.NEXT_TRACK,
                        track_id=track_id,
                        reason=last_reason,
                        gain_offset_db=extra,
                    )
                )
                break
            if previous_plan is not None and resolution.plan == previous_plan:
                # 減衰しても混合計画が変わらない（上限で頭打ち）。もう一度測っても同じ
                note(
                    FallbackEvent(
                        step=FallbackStep.NEXT_TRACK,
                        track_id=track_id,
                        reason=FallbackReason.SPEECH_MARGIN_TOO_SMALL,
                        gain_offset_db=extra,
                    )
                )
                last_reason = FallbackReason.SPEECH_MARGIN_TOO_SMALL
                break
            previous_plan = resolution.plan

            speech: SpeechIntelligibility | None = None
            if measure is not None:
                speech = await measure(
                    BgmRenderInput(mix=resolution.plan, track_paths=cand.data.track_paths)
                )
                if speech is None:
                    last_reason = FallbackReason.SPEECH_MARGIN_UNMEASURED
                    note(
                        FallbackEvent(
                            step=FallbackStep.NEXT_TRACK,
                            track_id=track_id,
                            reason=last_reason,
                            gain_offset_db=extra,
                        )
                    )
                    break
                if not speech.passed:
                    last_reason = FallbackReason.SPEECH_MARGIN_TOO_SMALL
                    if extra == 0 and FALLBACK_MAX_GAIN_STEPS > 0:
                        extra = FALLBACK_GAIN_STEP_DB
                        note(
                            FallbackEvent(
                                step=FallbackStep.GAIN_REDUCED,
                                track_id=track_id,
                                reason=last_reason,
                                gain_offset_db=extra,
                                min_speech_margin_centi_lu=speech.min_margin_centi_lu,
                            )
                        )
                        continue
                    note(
                        FallbackEvent(
                            step=FallbackStep.NEXT_TRACK,
                            track_id=track_id,
                            reason=last_reason,
                            gain_offset_db=extra,
                            min_speech_margin_centi_lu=speech.min_margin_centi_lu,
                        )
                    )
                    break
            decision = base.model_copy(
                update={
                    "track_id": track_id,
                    "applied_gain_offset_db": cand.applied_gain_offset_db,
                    "cue_sheet": cand.data.cue_sheet,
                    "cue_sheet_sha256": cue_sheet_sha256(cand.data.cue_sheet),
                    "min_speech_margin_centi_lu": (
                        speech.min_margin_centi_lu if speech is not None else None
                    ),
                    "fallback": tuple(events),
                }
            )
            return LadderResult(decision, resolution, cand.data, speech)

    if selection.candidates:
        note(FallbackEvent(step=FallbackStep.NO_BGM, reason=last_reason))
    return LadderResult(
        base.model_copy(
            update={
                "track_id": None,
                "applied_gain_offset_db": 0,
                "cue_sheet": None,
                "cue_sheet_sha256": None,
                "min_speech_margin_centi_lu": None,
                "fallback": tuple(events),
            }
        ),
        None,
        None,
        None,
    )


def without_bgm_after_loudness(
    decision: BgmSelectionDecision, *, brand_id: str, measured_min_margin: int | None
) -> BgmSelectionDecision:
    """BGM 入りの完成動画のラウドネスが範囲外だった: BGM 無しの決定へ置き換える（INV-155）。"""
    event = FallbackEvent(
        step=FallbackStep.NO_BGM_AFTER_LOUDNESS,
        track_id=decision.track_id,
        reason=FallbackReason.FINAL_LOUDNESS_OUT_OF_RANGE,
        gain_offset_db=decision.applied_gain_offset_db,
        min_speech_margin_centi_lu=measured_min_margin,
    )
    log_fallback(decision.episode_id, brand_id, event)
    return decision.model_copy(
        update={
            "track_id": None,
            "applied_gain_offset_db": 0,
            "cue_sheet": None,
            "cue_sheet_sha256": None,
            "min_speech_margin_centi_lu": None,
            "fallback": (*decision.fallback, event),
        }
    )


__all__ = [
    "LadderResult",
    "cue_sheet_sha256",
    "log_fallback",
    "log_selection",
    "reason_for_skip",
    "run_ladder",
    "without_bgm_after_loudness",
]
