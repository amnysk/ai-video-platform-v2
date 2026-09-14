"""Render 工程の Activity 名と入出力（ADR-0019）。

workflow（``workers/render``）と API が共有するのは**この名前と型だけ**である。

- 値はすべて Temporal の既定 converter で直列化できる素朴な型（str / int / bool / dataclass）
- 描画エンジン固有の語彙（バイナリ名・コマンドライン・物理パス）を置かない
- 既定値（並行数・timeout・heartbeat 等）の唯一の宣言元は
  ``contracts/render.py`` の ``DEFAULT_RENDER_*``
"""

from __future__ import annotations

from dataclasses import dataclass

from contracts.render import DEFAULT_RENDER_PROFILE_ID

# --------------------------------------------------------------------------- Activity 名

RENDER_ADMIT = "render_admit"
RENDER_FINAL_VIDEO = "render_final_video"
RENDER_MARK_READY = "render_mark_ready"
RENDER_RECORD_FAILURE = "render_record_failure"

#: 全 Activity 名。登録漏れ・綴り揺れの検査に使う。
RENDER_ACTIVITY_NAMES: tuple[str, ...] = (
    RENDER_ADMIT,
    RENDER_FINAL_VIDEO,
    RENDER_MARK_READY,
    RENDER_RECORD_FAILURE,
)

# ----------------------------------------------------------------- 実行ポリシー（ADR-0019 §10）

#: 描画 Activity の retry 上限（retryable の型だけ。needs_input / permanent は non_retryable）。
RENDER_MAX_ATTEMPTS = 3
#: 描画中に heartbeat を送る間隔の上限。heartbeat timeout（既定60秒）より十分短く取る。
RENDER_HEARTBEAT_INTERVAL_SECONDS = 10
#: cancel 時に子プロセスへ終了要求を送ってから強制終了までの猶予。
RENDER_CANCEL_GRACE_SECONDS = 5


# --------------------------------------------------------------------------- 状態系


@dataclass
class RenderAdmitRequest:
    episode_id: str
    workflow_id: str
    run_id: str


@dataclass
class RenderAdmitResult:
    #: False なら workflow は何もせず終わる。
    admitted: bool
    #: 判定時点の Episode 状態。Episode が無ければ空文字。
    status: str


@dataclass
class RenderMarkReadyRequest:
    episode_id: str
    workflow_id: str
    run_id: str


@dataclass
class RenderMarkReadyResult:
    status: str
    #: False: 入場トークンが一致せず何も書かなかった
    owned: bool = True


@dataclass
class RenderRecordFailureRequest:
    episode_id: str
    workflow_id: str
    run_id: str
    failure_class: str
    error_summary: str
    retry_exhausted: bool
    #: 失敗した job（無ければ空文字。job 作成前の失敗など）
    job_id: str = ""


@dataclass
class RenderFailureOutcome:
    episode_status: str
    owned: bool = True


# --------------------------------------------------------------------------- 描画


@dataclass
class RenderFinalVideoRequest:
    """入場トークン（workflow_id / run_id）と profile id だけを運ぶ。

    入力 Artifact は Activity が PostgreSQL の現行マニフェストから解決する（INV-8）。
    profile id はここで検証しない（contracts は domain を import できない）。Activity が
    ``UnknownRenderProfileError`` へ写像する。
    """

    episode_id: str
    workflow_id: str
    run_id: str
    render_profile_id: str = DEFAULT_RENDER_PROFILE_ID


@dataclass
class RenderFinalVideoResult:
    artifact_id: str
    sha256: str
    version: int
    #: 同じ input_hash の現行 final_video を再利用し、描画しなかった（INV-17 の証拠）
    skipped: bool
    #: この描画の job（record_failure が閉じる対象ではない。成功時の相関用）
    job_id: str = ""


__all__ = [
    "RENDER_ACTIVITY_NAMES",
    "RENDER_ADMIT",
    "RENDER_CANCEL_GRACE_SECONDS",
    "RENDER_FINAL_VIDEO",
    "RENDER_HEARTBEAT_INTERVAL_SECONDS",
    "RENDER_MARK_READY",
    "RENDER_MAX_ATTEMPTS",
    "RENDER_RECORD_FAILURE",
    "RenderAdmitRequest",
    "RenderAdmitResult",
    "RenderFailureOutcome",
    "RenderFinalVideoRequest",
    "RenderFinalVideoResult",
    "RenderMarkReadyRequest",
    "RenderMarkReadyResult",
    "RenderRecordFailureRequest",
]
