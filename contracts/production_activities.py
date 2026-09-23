"""Production 工程の Activity 名と入出力（ADR-0017）。

workflow（``workers/production``）とメディア別 worker（``workers/production_image`` 等）は
互いを import しない（INV-3）。両者が共有するのは**この名前と型だけ**である。

- 値はすべて Temporal の既定 converter で直列化できる素朴な型（str / int / bool / list / dataclass）
- provider 固有の語彙（モデル名・provider job id の形式・物理パス）を置かない。
  provider job の参照は予約台帳にだけ保存し、ここでは運ばない
"""

from __future__ import annotations

from dataclasses import dataclass, field

# --------------------------------------------------------------------------- Activity 名

PRODUCTION_ADMIT = "production_admit"
PRODUCTION_PLAN = "production_plan"
PRODUCTION_ASSEMBLE_MANIFEST = "production_assemble_manifest"
PRODUCTION_MARK_READY = "production_mark_ready"
PRODUCTION_RECORD_FAILURE = "production_record_failure"

IMAGE_SUBMIT = "image_submit"
IMAGE_AWAIT = "image_await"
VOICE_GENERATE = "voice_generate"
VIDEO_SUBMIT = "video_submit"
VIDEO_AWAIT = "video_await"

#: 全 Activity 名。登録漏れ・綴り揺れの検査に使う。
PRODUCTION_ACTIVITY_NAMES: tuple[str, ...] = (
    PRODUCTION_ADMIT,
    PRODUCTION_PLAN,
    PRODUCTION_ASSEMBLE_MANIFEST,
    PRODUCTION_MARK_READY,
    PRODUCTION_RECORD_FAILURE,
    IMAGE_SUBMIT,
    IMAGE_AWAIT,
    VOICE_GENERATE,
    VIDEO_SUBMIT,
    VIDEO_AWAIT,
)

# ----------------------------------------------------------------- 実行ポリシー（ADR-0017）

#: 課金 submit は Temporal に retry させない（INV-15）。retry はラウンドとして台帳を通す
SUBMIT_MAX_ATTEMPTS = 1

#: 実行予算と並行数の既定値の唯一の宣言元（Settings / API / workflow 入力がここを参照する）
DEFAULT_IMAGE_CONCURRENCY = 2
DEFAULT_VOICE_CONCURRENCY = 1
DEFAULT_VIDEO_CONCURRENCY = 1
DEFAULT_IMAGE_MAX_ROUNDS = 3
DEFAULT_VIDEO_MAX_ROUNDS = 2
DEFAULT_AWAIT_REEXECUTIONS = 3
#: await は provider job 参照に対して冪等（再送しない）なので retry してよい。
AWAIT_MAX_ATTEMPTS = 5
AWAIT_START_TO_CLOSE_SECONDS = 40 * 60
AWAIT_HEARTBEAT_TIMEOUT_SECONDS = 90
#: ローカル非課金の音声合成（INV-15 の対象外。ADR-0017 の限定例外）。
VOICE_MAX_ATTEMPTS = 3

# ------------------------------------------------------------ provider 認可障害の抑止（ADR-0030）

#: 同じ provider の未解決 ``provider_auth_incidents`` をこの分の過去だけ数える。
AUTH_INCIDENT_WINDOW_MINUTES = 10
#: ウィンドウ内でこの件数以上の未解決 incident があれば、新規 submit を止める（予約 INSERT の前）。
AUTH_INCIDENT_SUPPRESSION_THRESHOLD = 3


# --------------------------------------------------------------------------- 状態系


@dataclass
class ProductionAdmitRequest:
    episode_id: str
    workflow_id: str
    run_id: str


@dataclass
class ProductionAdmitResult:
    #: False なら workflow は何もせず終わる。
    admitted: bool
    #: 判定時点の Episode 状態。Episode が無ければ空文字。
    status: str


@dataclass
class ProductionPlanRequest:
    episode_id: str
    workflow_id: str
    run_id: str


@dataclass
class SceneImageWork:
    scene_id: str


@dataclass
class SceneVideoWork:
    scene_id: str
    requested_duration_ms: int


@dataclass
class SceneVoiceWork:
    script_scene_id: str
    storyboard_scene_ids: list[str]


@dataclass
class ProductionPlan:
    """workflow が回すシーン作業の一覧。順序は storyboard / 台本の順。"""

    storyboard_artifact_id: str
    storyboard_sha256: str
    script_artifact_id: str
    script_sha256: str
    images: list[SceneImageWork] = field(default_factory=list)
    videos: list[SceneVideoWork] = field(default_factory=list)
    voices: list[SceneVoiceWork] = field(default_factory=list)


@dataclass
class ProductionAssembleRequest:
    episode_id: str
    workflow_id: str
    run_id: str
    storyboard_artifact_id: str
    script_artifact_id: str


@dataclass
class ProductionMarkReadyRequest:
    episode_id: str
    workflow_id: str
    run_id: str


@dataclass
class ProductionMarkReadyResult:
    status: str
    #: False: 入場トークンが一致せず何も書かなかった
    owned: bool = True


@dataclass
class ProductionRecordFailureRequest:
    episode_id: str
    workflow_id: str
    run_id: str
    failure_class: str
    error_summary: str
    retry_exhausted: bool
    #: 失敗した job（無ければ空文字。plan 前の失敗など）
    job_id: str = ""


@dataclass
class ProductionFailureOutcome:
    episode_status: str
    owned: bool = True


# --------------------------------------------------------------------------- メディア系


@dataclass
class SceneArtifactResult:
    """シーン Artifact 1件の結果。"""

    artifact_id: str
    object_key: str
    sha256: str
    #: 生成器を呼ばずに既存 Artifact を再利用したか（INV-17 の証拠）
    reused: bool


@dataclass
class ImageSubmitRequest:
    episode_id: str
    workflow_id: str
    run_id: str
    scene_id: str
    storyboard_artifact_id: str
    round: int


@dataclass
class SubmitResult:
    """submit の結果。``artifact`` があれば既存を再利用したので await は不要。

    provider job の参照は運ばない（予約台帳にだけある）。await は ``reservation_id`` で引く。
    """

    reservation_id: str
    artifact: SceneArtifactResult | None = None


@dataclass
class ImageAwaitRequest:
    episode_id: str
    workflow_id: str
    run_id: str
    scene_id: str
    storyboard_artifact_id: str
    reservation_id: str


@dataclass
class VoiceGenerateRequest:
    episode_id: str
    workflow_id: str
    run_id: str
    script_scene_id: str
    storyboard_scene_ids: list[str]
    storyboard_artifact_id: str
    script_artifact_id: str


@dataclass
class VideoSubmitRequest:
    episode_id: str
    workflow_id: str
    run_id: str
    scene_id: str
    storyboard_artifact_id: str
    source_image_artifact_id: str
    requested_duration_ms: int
    round: int


@dataclass
class VideoAwaitRequest:
    episode_id: str
    workflow_id: str
    run_id: str
    scene_id: str
    storyboard_artifact_id: str
    source_image_artifact_id: str
    requested_duration_ms: int
    reservation_id: str


__all__ = [
    "AUTH_INCIDENT_SUPPRESSION_THRESHOLD",
    "AUTH_INCIDENT_WINDOW_MINUTES",
    "AWAIT_HEARTBEAT_TIMEOUT_SECONDS",
    "AWAIT_MAX_ATTEMPTS",
    "AWAIT_START_TO_CLOSE_SECONDS",
    "IMAGE_AWAIT",
    "IMAGE_SUBMIT",
    "PRODUCTION_ACTIVITY_NAMES",
    "PRODUCTION_ADMIT",
    "PRODUCTION_ASSEMBLE_MANIFEST",
    "PRODUCTION_MARK_READY",
    "PRODUCTION_PLAN",
    "PRODUCTION_RECORD_FAILURE",
    "SUBMIT_MAX_ATTEMPTS",
    "DEFAULT_IMAGE_CONCURRENCY",
    "DEFAULT_VOICE_CONCURRENCY",
    "DEFAULT_VIDEO_CONCURRENCY",
    "DEFAULT_IMAGE_MAX_ROUNDS",
    "DEFAULT_VIDEO_MAX_ROUNDS",
    "DEFAULT_AWAIT_REEXECUTIONS",
    "VIDEO_AWAIT",
    "VIDEO_SUBMIT",
    "VOICE_GENERATE",
    "VOICE_MAX_ATTEMPTS",
    "ImageAwaitRequest",
    "ImageSubmitRequest",
    "ProductionAdmitRequest",
    "ProductionAdmitResult",
    "ProductionAssembleRequest",
    "ProductionFailureOutcome",
    "ProductionMarkReadyRequest",
    "ProductionMarkReadyResult",
    "ProductionPlan",
    "ProductionPlanRequest",
    "ProductionRecordFailureRequest",
    "SceneArtifactResult",
    "SceneImageWork",
    "SceneVideoWork",
    "SceneVoiceWork",
    "SubmitResult",
    "VideoAwaitRequest",
    "VideoSubmitRequest",
    "VoiceGenerateRequest",
]
