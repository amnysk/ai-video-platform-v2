"""BGM の音源ソースと自動収集の契約（ADR-0087 §1〜§3）。**唯一の定義**（AGENTS.md §8）。

- 「検索してよい」と「自動で取得してよい」は別の事実として持つ（無料ダウンロード可 ≠ 利用可）
- 自動承認の規則の版・上限の数値はここだけに置き、収集・選曲・テストが import する
- 規約の根拠は ``docs/research/bgm-sources-2026-11.md``。
  規約が変わったら表と文書を同じコミットで更新する
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from typing import Literal

from pydantic import Field, field_validator

from contracts.artifact_refs import SHA256_HEX_PATTERN, FrozenModel

#: 自動承認の規則の版。規則（``domain.music.license_policy``）を変えたら上げる。
#: ``policy`` 承認の曲は版が一致しなければ選曲に出ない（古い規則の自動承認を引き継がない。INV-151）
BGM_LICENSE_POLICY_VERSION = "commons-cc0-1"

#: 収集を有効にする設定キー（``Settings.bgm_acquisition_enabled``）。既定 OFF（ADR-0087 §10）
BGM_ACQUISITION_SETTING_KEY = "bgm_acquisition_enabled"
BGM_ACQUISITION_TASK_QUEUE = "bgm-acquisition"


class MusicSourceId(StrEnum):
    WIKIMEDIA_COMMONS = "wikimedia_commons"
    FREE_MUSIC_ARCHIVE = "free_music_archive"
    PIXABAY_MUSIC = "pixabay_music"
    CCMIXTER = "ccmixter"
    INCOMPETECH = "incompetech"
    #: 所有者が手で供給した曲（既存の catalog.json からの取込を含む）
    OWNER_SUPPLIED = "owner_supplied"


@dataclass(frozen=True, slots=True)
class MusicSourcePolicy:
    source: MusicSourceId
    #: 規約上、検索（候補の提示）をしてよいか。``False`` なら候補にも出さない
    search_allowed: bool
    #: 規約上、プログラムからの取得（API と原本 URL）が許され、ライセンスを機械的に確定できるか
    auto_acquire_allowed: bool
    #: 規約を確認した日（ISO 日付）。``docs/research/bgm-sources-2026-11.md`` と一致させる
    terms_checked_on: str


#: ソースごとの方針（ADR-0087 §1 の表）。**ここが唯一の定義**。
SOURCE_POLICIES: dict[MusicSourceId, MusicSourcePolicy] = {
    MusicSourceId.WIKIMEDIA_COMMONS: MusicSourcePolicy(
        MusicSourceId.WIKIMEDIA_COMMONS, True, True, "2026-11-02"
    ),
    MusicSourceId.FREE_MUSIC_ARCHIVE: MusicSourcePolicy(
        MusicSourceId.FREE_MUSIC_ARCHIVE, True, False, "2026-11-02"
    ),
    MusicSourceId.PIXABAY_MUSIC: MusicSourcePolicy(
        MusicSourceId.PIXABAY_MUSIC, True, False, "2026-11-02"
    ),
    MusicSourceId.CCMIXTER: MusicSourcePolicy(MusicSourceId.CCMIXTER, True, False, "2026-11-02"),
    MusicSourceId.INCOMPETECH: MusicSourcePolicy(
        MusicSourceId.INCOMPETECH, True, False, "2026-11-02"
    ),
    MusicSourceId.OWNER_SUPPLIED: MusicSourcePolicy(
        MusicSourceId.OWNER_SUPPLIED, False, False, "2026-11-02"
    ),
}


def auto_acquire_allowed(source: MusicSourceId) -> bool:
    policy = SOURCE_POLICIES.get(source)
    return policy is not None and policy.auto_acquire_allowed


# ---------------------------------------------------------------- 上限（ADR-0087 §3）

ACQUISITION_MAX_SEARCHES_PER_RUN = 4
ACQUISITION_MAX_DOWNLOADS_PER_RUN = 3
ACQUISITION_MAX_BYTES_PER_FILE = 40 * 1024 * 1024
ACQUISITION_MAX_BYTES_PER_RUN = 120 * 1024 * 1024
ACQUISITION_MIN_DURATION_MS = 30_000
ACQUISITION_MAX_DURATION_MS = 15 * 60_000
ACQUISITION_RUN_TIMEOUT_SECONDS = 600
ACQUISITION_HTTP_TIMEOUT_SECONDS = 30
#: 同じ需要での収集は日に 1 回まで
ACQUISITION_MIN_INTERVAL_SECONDS = 24 * 3600
#: 受け付けるコンテナ（拡張子。ffprobe で実体も確かめる）
ACQUISITION_ALLOWED_EXTENSIONS: tuple[str, ...] = ("flac", "ogg", "oga", "mp3", "wav", "opus")


# ---------------------------------------------------------------- ライセンス判定（ADR-0087 §2）


class LicenseDecisionKind(StrEnum):
    AUTO_APPROVE = "auto_approve"
    REVIEW = "review"
    REJECT = "reject"


class LicenseReason(StrEnum):
    """判定の理由コード（固定語彙。ログ・DB にそのまま出してよい）。"""

    CC0_OWN_WORK = "cc0_own_work"
    SOURCE_NOT_AUTO_ACQUIRABLE = "source_not_auto_acquirable"
    ATTRIBUTION_LICENSE_NEEDS_REVIEW = "attribution_license_needs_review"
    SHARE_ALIKE_NEEDS_REVIEW = "share_alike_needs_review"
    PUBLIC_DOMAIN_NEEDS_REVIEW = "public_domain_needs_review"
    NOT_OWN_WORK = "not_own_work"
    METADATA_INCOMPLETE = "metadata_incomplete"
    METADATA_CONFLICT = "metadata_conflict"
    RESTRICTIONS_PRESENT = "restrictions_present"
    DELETION_OR_DISPUTE = "deletion_or_dispute"
    NON_COMMERCIAL = "non_commercial"
    NO_DERIVATIVES = "no_derivatives"
    LICENSE_UNKNOWN = "license_unknown"
    SOURCE_UNKNOWN = "source_unknown"


ContentIdRisk = Literal["unknown", "low", "elevated"]
LicenseApproverKind = Literal["owner", "policy"]


class LicenseFacts(FrozenModel):
    """保存した証拠（API の応答）から**機械的に**取り出した事実。推定を入れない。

    文字列は正規化前の原文（判定側が正規化する）。欠けている項目は ``None``。
    """

    source: MusicSourceId
    source_page_url: str = Field(min_length=1)
    original_file_url: str | None = None
    license_short_name: str | None = None
    license_url: str | None = None
    #: Commons の ``extmetadata.License``（機械可読の短い識別子。例 ``cc0``）
    license_code: str | None = None
    usage_terms: str | None = None
    artist: str | None = None
    credit: str | None = None
    attribution_required: bool | None = None
    copyrighted: bool | None = None
    restrictions: str | None = None
    #: 削除依頼・著作権の係争などのカテゴリ・テンプレートが付いているか
    deletion_or_dispute: bool = False
    title: str | None = None
    #: 証拠（正準 JSON）の sha256。判定はこの証拠に結び付く
    evidence_sha256: str = Field(pattern=SHA256_HEX_PATTERN)


@dataclass(frozen=True, slots=True)
class LicenseDecision:
    kind: LicenseDecisionKind
    reasons: tuple[LicenseReason, ...]
    policy_version: str = BGM_LICENSE_POLICY_VERSION


# ------------------------------------------------ 収集の監査（bgm_acquisition_runs）


class AcquisitionOutcome(StrEnum):
    REGISTERED_APPROVED = "registered_approved"
    REGISTERED_QUARANTINED = "registered_quarantined"
    DUPLICATE = "duplicate"
    LICENSE_REJECTED = "license_rejected"
    DOWNLOAD_FAILED = "download_failed"
    VALIDATION_FAILED = "validation_failed"
    LIMIT_REACHED = "limit_reached"
    SOURCE_UNAVAILABLE = "source_unavailable"


class AcquisitionCandidateResult(FrozenModel):
    source: MusicSourceId
    source_page_url: str
    outcome: AcquisitionOutcome
    license_decision: LicenseDecisionKind | None = None
    license_reasons: tuple[LicenseReason, ...] = ()
    file_sha256: str | None = Field(default=None, pattern=SHA256_HEX_PATTERN)
    bytes_downloaded: int = Field(default=0, ge=0)
    track_id: str | None = None


class AcquisitionRunSummary(FrozenModel):
    demand_sha256: str = Field(pattern=SHA256_HEX_PATTERN)
    started_at: datetime
    finished_at: datetime
    searches: int = Field(ge=0)
    candidates: tuple[AcquisitionCandidateResult, ...] = ()

    @field_validator("started_at", "finished_at")
    @classmethod
    def _aware(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("datetime must be timezone-aware")
        return value


__all__ = [
    "ACQUISITION_ALLOWED_EXTENSIONS",
    "ACQUISITION_HTTP_TIMEOUT_SECONDS",
    "ACQUISITION_MAX_BYTES_PER_FILE",
    "ACQUISITION_MAX_BYTES_PER_RUN",
    "ACQUISITION_MAX_DOWNLOADS_PER_RUN",
    "ACQUISITION_MAX_DURATION_MS",
    "ACQUISITION_MAX_SEARCHES_PER_RUN",
    "ACQUISITION_MIN_DURATION_MS",
    "ACQUISITION_MIN_INTERVAL_SECONDS",
    "ACQUISITION_RUN_TIMEOUT_SECONDS",
    "BGM_ACQUISITION_SETTING_KEY",
    "BGM_ACQUISITION_TASK_QUEUE",
    "BGM_LICENSE_POLICY_VERSION",
    "SOURCE_POLICIES",
    "AcquisitionCandidateResult",
    "AcquisitionOutcome",
    "AcquisitionRunSummary",
    "ContentIdRisk",
    "LicenseApproverKind",
    "LicenseDecision",
    "LicenseDecisionKind",
    "LicenseFacts",
    "LicenseReason",
    "MusicSourceId",
    "MusicSourcePolicy",
    "auto_acquire_allowed",
]
