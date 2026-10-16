"""Upload 工程の Activity 名と入出力（ADR-0020）。

workflow（``workers/upload``）と API が共有するのは**この名前と型だけ**である。

- 値はすべて Temporal の既定 converter で直列化できる素朴な型（str / int / bool / dataclass）
- session URI・OAuth トークン・チャンネルの認証情報を運ばない（INV-20）
- 既定値の唯一の宣言元は ``contracts/upload.py`` の ``DEFAULT_UPLOAD_*``
"""

from __future__ import annotations

from dataclasses import dataclass

from contracts.upload import DEFAULT_UPLOAD_HEARTBEAT_INTERVAL_SECONDS, DEFAULT_UPLOAD_MAX_ATTEMPTS

# --------------------------------------------------------------------------- Activity 名

UPLOAD_ADMIT = "upload_admit"
UPLOAD_FINAL_VIDEO = "upload_final_video"
UPLOAD_AWAIT_PROCESSING = "upload_await_processing"
UPLOAD_MARK_UPLOADED = "upload_mark_uploaded"
UPLOAD_RECORD_FAILURE = "upload_record_failure"

#: 全 Activity 名。登録漏れ・綴り揺れの検査に使う。
UPLOAD_ACTIVITY_NAMES: tuple[str, ...] = (
    UPLOAD_ADMIT,
    UPLOAD_FINAL_VIDEO,
    UPLOAD_AWAIT_PROCESSING,
    UPLOAD_MARK_UPLOADED,
    UPLOAD_RECORD_FAILURE,
)

# ----------------------------------------------------------------- 実行ポリシー（ADR-0020 §10）

UPLOAD_MAX_ATTEMPTS = DEFAULT_UPLOAD_MAX_ATTEMPTS
UPLOAD_HEARTBEAT_INTERVAL_SECONDS = DEFAULT_UPLOAD_HEARTBEAT_INTERVAL_SECONDS


# --------------------------------------------------------------------------- 状態系


@dataclass
class UploadAdmitRequest:
    episode_id: str
    workflow_id: str
    run_id: str


@dataclass
class UploadAdmitResult:
    #: False なら workflow は何もせず終わる。
    admitted: bool
    #: 判定時点の Episode 状態。Episode が無ければ空文字。
    status: str
    #: 現行 final_video のサイズから見積もった start_to_close（秒）。0 は既定値。
    upload_timeout_seconds: int = 0


@dataclass
class UploadMarkUploadedRequest:
    episode_id: str
    workflow_id: str
    run_id: str


@dataclass
class UploadMarkUploadedResult:
    status: str
    #: False: 入場トークンが一致せず何も書かなかった
    owned: bool = True


@dataclass
class UploadRecordFailureRequest:
    episode_id: str
    workflow_id: str
    run_id: str
    failure_class: str
    #: redact 済みの要約（session URI・トークンを含めない）
    error_summary: str
    retry_exhausted: bool
    job_id: str = ""


@dataclass
class UploadFailureOutcome:
    episode_status: str
    owned: bool = True


# --------------------------------------------------------------------------- 投稿


@dataclass
class UploadFinalVideoRequest:
    """入場トークンだけを運ぶ。

    入力の final_video は Activity が PostgreSQL から解決する（INV-8）。
    """

    episode_id: str
    workflow_id: str
    run_id: str


@dataclass
class UploadFinalVideoResult:
    #: upload_receipt Artifact
    artifact_id: str
    sha256: str
    version: int
    video_id: str
    #: 既存の受領 / SPENT 予約を再利用し、YouTube を呼ばなかった（INV-14 の証拠）
    skipped: bool
    #: ``contracts.upload.UploadReconciledBy`` の値
    reconciled_by: str
    job_id: str = ""


@dataclass
class UploadAwaitProcessingRequest:
    """投稿済み動画の処理状態を1回照会する（ADR-0022）。待ちは workflow の RetryPolicy が持つ。"""

    episode_id: str
    workflow_id: str
    run_id: str
    video_id: str


@dataclass
class UploadAwaitProcessingResult:
    video_id: str
    #: ``status.uploadStatus``（processed）
    upload_status: str
    #: ``domain.upload.processing.ProcessingVerdict.reason``
    reason: str
    processing_status: str = ""


__all__ = [
    "UPLOAD_ACTIVITY_NAMES",
    "UPLOAD_ADMIT",
    "UPLOAD_AWAIT_PROCESSING",
    "UploadAwaitProcessingRequest",
    "UploadAwaitProcessingResult",
    "UPLOAD_FINAL_VIDEO",
    "UPLOAD_HEARTBEAT_INTERVAL_SECONDS",
    "UPLOAD_MARK_UPLOADED",
    "UPLOAD_MAX_ATTEMPTS",
    "UPLOAD_RECORD_FAILURE",
    "UploadAdmitRequest",
    "UploadAdmitResult",
    "UploadFailureOutcome",
    "UploadFinalVideoRequest",
    "UploadFinalVideoResult",
    "UploadMarkUploadedRequest",
    "UploadMarkUploadedResult",
    "UploadRecordFailureRequest",
]
