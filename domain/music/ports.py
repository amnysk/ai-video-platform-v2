"""BGM カタログの永続化の port（ADR-0087 §3 / §4）。収集（書き手）と選曲（読み手）が共有する。

実装は ``infrastructure/db/bgm_repositories.py``（PostgreSQL）。domain はこの形だけを知る。
メソッドは全て非同期で、1 呼び出し = 1 トランザクション（呼び出し側は session を持たない）。
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Protocol

from contracts.music import BgmTrack
from contracts.music_selection import BgmSelectionDecision
from contracts.music_sources import AcquisitionRunSummary


@dataclass(frozen=True, slots=True)
class CatalogEntry:
    """カタログの 1 行。``track`` が契約、残りは DB にだけある監査・解析の列。"""

    track: BgmTrack
    acquired_from: str
    acquired_at: datetime
    evidence_object_key: str | None
    #: 実測（ラウドネス・true peak・尺・テンポ推定）。客観値
    analysis: Mapping[str, Any]
    #: 推定（ジャンル・感情・雰囲気）。ライセンスの事実と混ぜない
    inferred_tags: Mapping[str, Any]
    last_verified_at: datetime | None


class BgmCatalogRepository(Protocol):
    # ---- 読み（選曲）
    async def list_entries(self) -> tuple[CatalogEntry, ...]: ...

    async def find_by_sha256(self, file_sha256: str) -> CatalogEntry | None: ...

    #: track_id → 直近 ``window`` 件の選曲で使われた回数（BGM 無しの決定は数えない）
    async def recent_usage(self, *, window: int) -> dict[str, int]: ...

    # ---- 書き（収集・取込）
    #: 隔離（``status=quarantined`` / ``license_status=unverified``）で登録する。同じ sha256 が
    #: あれば何も書かず既存を返す（INV-133）。``track`` が隔離でなければ ValueError
    async def register_quarantined(
        self,
        track: BgmTrack,
        *,
        acquired_from: str,
        acquired_at: datetime,
        evidence_object_key: str | None,
        analysis: Mapping[str, Any],
        inferred_tags: Mapping[str, Any],
    ) -> tuple[CatalogEntry, bool]: ...

    #: 隔離中の曲を承認済み・active へ進める（規則または所有者の承認）。承認は ``approved`` の
    #: 契約（証拠 sha256・確認時刻・approver_kind、policy なら版）を満たす ``track`` で置き換える
    async def promote(self, track: BgmTrack, *, verified_at: datetime) -> CatalogEntry: ...

    async def record_acquisition_run(self, summary: AcquisitionRunSummary) -> None: ...

    async def last_acquisition_at(self, demand_sha256: str) -> datetime | None: ...

    #: 収集の需要（適切な曲が無かった要件）を積む。同じ hash は 1 行（冪等）
    async def request_acquisition(
        self, demand_sha256: str, requirements_json: Mapping[str, Any], *, requested_at: datetime
    ) -> None: ...

    async def pending_demands(self, *, limit: int) -> tuple[tuple[str, Mapping[str, Any]], ...]: ...

    # ---- 選曲の決定（Episode ごとに 1 行）
    async def get_selection(self, episode_id: str) -> BgmSelectionDecision | None: ...

    #: 無ければ挿入し、あれば**既存を返す**（後勝ちしない。INV-153）
    async def save_selection_once(
        self, decision: BgmSelectionDecision, *, decided_at: datetime
    ) -> BgmSelectionDecision: ...

    #: フォールバックの結果（適用 gain・実測・梯子の経過）で決定を確定する。確定済みなら既存を返す
    async def finalize_selection(
        self, decision: BgmSelectionDecision, *, finalized_at: datetime
    ) -> BgmSelectionDecision: ...


class FinalizableCatalogRepository(BgmCatalogRepository, Protocol):
    """``BgmCatalogRepository`` に確定状態の読み出しを足したもの（Render の選曲が使う）。

    ``BgmCatalogRepository`` 自体は変えない（収集側の fake が壊れない）。
    """

    #: その Episode の選曲が確定済み（梯子の結果を保存済み）か。行が無ければ False
    async def is_selection_finalized(self, episode_id: str) -> bool: ...


__all__ = ["BgmCatalogRepository", "CatalogEntry", "FinalizableCatalogRepository"]
