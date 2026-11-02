"""``SelectingBgmSource`` の DB 版（ADR-0087 §5 / §6、INV-153）。

Render の内側で Episode ごとに 1 回だけ選曲を決めて ``bgm_selections`` へ保存する。

- 確定済みの選曲があればそれを返す（再選曲しない）。cue sheet はその決定のものをそのまま使う
- 無ければ要件を導出して採点し、``save_selection_once`` で保存する（同時実行でも 1 行）。適切な曲が
  無いときは**収集の需要を積むだけ**で、ネットワークには触れず、待たない
- 原本は MinIO（``music/originals/<sha256>.<ext>``）から作業領域のキャッシュへ取り出し、sha256 を
  照合する。一致しなければ使わない（``SOURCE_UNREADABLE`` -> 次の曲 / BGM 無し）
- fal・provider・production のコードは import しない
"""

from __future__ import annotations

import logging
from datetime import datetime
from pathlib import Path

from contracts.music import BgmTrack
from contracts.music_render import BGM_VIDEO_FORMAT_BY_PROFILE_ID
from contracts.music_selection import (
    DEFAULT_BRAND_POLICY,
    FALLBACK_MAX_TRACKS,
    BgmSelectionDecision,
    BrandMusicPolicy,
    FallbackReason,
)
from domain.music.auto_select import ladder_candidates, select_for_episode
from domain.music.placement import plan_cue_sheet
from domain.music.ports import FinalizableCatalogRepository
from domain.music.requirements import derive_requirements
from domain.music.scoring import passes_license_gate
from domain.render.ports import (
    BgmCandidate,
    BgmRenderContext,
    BgmRenderSelection,
    BgmSourceData,
)
from infrastructure.render.binary import file_sha256
from infrastructure.storage.artifact_store import ArtifactStore

logger = logging.getLogger(__name__)

#: 直近の使用回数を数える選曲の件数（新鮮さの点に効く）
RECENT_USAGE_WINDOW = 20
#: 作業領域の中の原本キャッシュ置き場
ORIGINALS_DIR_NAME = "bgm-originals"


class DbBgmSource:
    def __init__(
        self,
        repository: FinalizableCatalogRepository,
        store: ArtifactStore,
        *,
        brand: BrandMusicPolicy = DEFAULT_BRAND_POLICY,
    ) -> None:
        self._repo = repository
        self._store = store
        self._brand = brand

    # ------------------------------------------------------------------ load

    async def load_for_render(self, ctx: BgmRenderContext) -> BgmRenderSelection:
        existing = await self._repo.get_selection(ctx.episode_id)
        if existing is not None and await self._repo.is_selection_finalized(ctx.episode_id):
            return await self._finalized(ctx, existing)

        if existing is None:
            fmt = BGM_VIDEO_FORMAT_BY_PROFILE_ID.get(ctx.plan.profile.profile_id)
            if fmt is None:  # 形式を推測しない（ライセンス検査が形式に依る）
                fmt = "shorts" if ctx.plan.profile.profile_id.startswith("shorts") else "longform"
            requirements = derive_requirements(
                ctx.script, ctx.storyboard, fmt=fmt, brand=self._brand
            )
            entries = await self._repo.list_entries()
            usage = await self._repo.recent_usage(window=RECENT_USAGE_WINDOW)
            decision = select_for_episode(
                ctx.episode_id, requirements, entries, usage, ctx.now, brand=self._brand
            )
            existing = await self._repo.save_selection_once(decision, decided_at=ctx.now)
            if existing.acquisition_requested:
                # 待たない・ネットワークに触れない。次の Episode から使える曲を増やすための需要だけ
                await self._repo.request_acquisition(
                    existing.requirements_sha256,
                    existing.requirements.model_dump(mode="json"),
                    requested_at=ctx.now,
                )
        return BgmRenderSelection(
            decision=existing,
            finalized=False,
            candidates=ladder_candidates(existing, limit=FALLBACK_MAX_TRACKS),
        )

    async def _finalized(
        self, ctx: BgmRenderContext, decision: BgmSelectionDecision
    ) -> BgmRenderSelection:
        if decision.track_id is None or decision.cue_sheet is None:
            return BgmRenderSelection(decision=decision, finalized=True, data=BgmSourceData(None))
        entries = {e.track.track_id: e.track for e in await self._repo.list_entries()}
        track = entries.get(decision.track_id)
        if track is None:
            return BgmRenderSelection(
                decision=decision, finalized=True, data=BgmSourceData(decision.cue_sheet)
            )
        fmt = BGM_VIDEO_FORMAT_BY_PROFILE_ID.get(ctx.plan.profile.profile_id)
        # 確定済みの決定の再利用でも、描画の直前に同じ門（INV-97 + INV-151 の規則の版）を通す
        # （独立レビュー F3: 確定後に規則の版が上がった自動承認の曲を再試行で鳴らさない）
        if fmt is None or not passes_license_gate(track, now=ctx.now, fmt=fmt):
            logger.warning(
                "bgm not used episode=%s track=%s reason=%s (finalized decision kept)",
                ctx.episode_id,
                track.track_id,
                FallbackReason.LICENSE_RECHECK_FAILED.value,
            )
            return BgmRenderSelection(decision=decision, finalized=True, data=BgmSourceData(None))
        path = await self._fetch(ctx, track)
        shas = {track.track_id: file_sha256(path) if path is not None else None}
        return BgmRenderSelection(
            decision=decision,
            finalized=True,
            data=BgmSourceData(
                cue_sheet=decision.cue_sheet,
                catalog=(track,),
                track_paths={track.track_id: path} if path is not None else {},
                file_sha256_by_track=shas,
            ),
        )

    # ------------------------------------------------------------------ ladder の 1 段

    async def prepare_candidate(
        self, ctx: BgmRenderContext, track_id: str, *, extra_gain_offset_db: int
    ) -> BgmCandidate:
        entries = {e.track.track_id: e.track for e in await self._repo.list_entries()}
        track = entries.get(track_id)
        fmt = BGM_VIDEO_FORMAT_BY_PROFILE_ID.get(ctx.plan.profile.profile_id)
        if track is None:
            return BgmCandidate(failure=FallbackReason.SOURCE_UNREADABLE)
        if fmt is None:
            return BgmCandidate(failure=FallbackReason.PLACEMENT_REFUSED)
        # 描画の直前に、選曲の後で曲が失効・撤回されていないか再検査する（INV-114 と同じ門）
        if not passes_license_gate(track, now=ctx.now, fmt=fmt):
            return BgmCandidate(failure=FallbackReason.LICENSE_RECHECK_FAILED)
        placed = plan_cue_sheet(ctx.plan, track, extra_gain_offset_db=extra_gain_offset_db)
        if placed.cue_sheet is None:
            return BgmCandidate(failure=FallbackReason.PLACEMENT_REFUSED)
        path = await self._fetch(ctx, track)
        if path is None:
            return BgmCandidate(failure=FallbackReason.SOURCE_UNREADABLE)
        return BgmCandidate(
            data=BgmSourceData(
                cue_sheet=placed.cue_sheet,
                catalog=(track,),
                track_paths={track.track_id: path},
                file_sha256_by_track={track.track_id: file_sha256(path)},
            ),
            applied_gain_offset_db=track.mix_gain_offset_db + extra_gain_offset_db,
        )

    async def finalize(
        self, decision: BgmSelectionDecision, *, finalized_at: datetime
    ) -> BgmSelectionDecision:
        return await self._repo.finalize_selection(decision, finalized_at=finalized_at)

    # ------------------------------------------------------------------ 原本

    async def _fetch(self, ctx: BgmRenderContext, track: BgmTrack) -> Path | None:
        """原本を作業領域へ取り出して sha256 を照合する。照合できなければ ``None``（使わない）。"""
        suffix = Path(track.object_key).suffix
        cache = ctx.work_dir / ORIGINALS_DIR_NAME
        path = cache / f"{track.file_sha256}{suffix}"
        try:
            cache.mkdir(parents=True, exist_ok=True)
            if path.is_file() and file_sha256(path) == track.file_sha256:
                return path
            actual = await self._store.download_to(track.object_key, path)
        except (KeyError, OSError) as exc:
            logger.warning(
                "bgm original unreadable track=%s error=%s", track.track_id, type(exc).__name__
            )
            return None
        if actual != track.file_sha256:
            logger.warning("bgm original sha256 mismatch track=%s", track.track_id)
            path.unlink(missing_ok=True)
            return None
        return path


__all__ = ["ORIGINALS_DIR_NAME", "RECENT_USAGE_WINDOW", "DbBgmSource"]
