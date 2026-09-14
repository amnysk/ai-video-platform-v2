"""実行時設定。secretはここから外へ出さない（INV-20）。"""

from __future__ import annotations

from pydantic import SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict

from contracts.production_activities import (
    DEFAULT_AWAIT_REEXECUTIONS,
    DEFAULT_IMAGE_CONCURRENCY,
    DEFAULT_IMAGE_MAX_ROUNDS,
    DEFAULT_VIDEO_CONCURRENCY,
    DEFAULT_VIDEO_MAX_ROUNDS,
    DEFAULT_VOICE_CONCURRENCY,
)


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    database_url: str = "postgresql+psycopg://avp:change-me@localhost:5432/avp"

    minio_endpoint: str = "http://localhost:9000"
    minio_access_key: str = "minioadmin"
    minio_secret_key: str = "change-me"
    minio_bucket: str = "artifacts"

    temporal_address: str = "localhost:7233"
    temporal_namespace: str = "default"
    temporal_task_queue: str = "episode-skeleton"

    # --- Codex CLI（台本生成） ---
    #: 実行ファイルの絶対パス。空なら ``shutil.which("codex")`` で解決する。
    #: nvm 配下の codex は PATH に無いことがあるので明示できるようにしてある。
    codex_binary: str = ""
    codex_model: str = ""  # 空なら codex の既定（~/.codex/config.toml）に従う
    #: CLI 側にタイムアウトオプションが無いので、Python 側の上限がこれだけ。
    codex_timeout_seconds: int = 900
    #: codex を ``-C`` で走らせる作業ディレクトリ（sandbox は read-only）。
    codex_workspace: str = "."

    # --- storyboard（ADR-0015 / ADR-0016） ---
    #: 一時作業領域の root。正式Artifactの保存先ではない（docs/operations/work-directories.md）。
    ai_video_work_root: str = "/mnt/minio-hdd/ai-video-work"
    #: OpenMontage の共有 checkout。**読み取り専用**。未設定なら storyboard 生成器を組めない。
    openmontage_repo_path: str | None = None
    #: 仕様 blob を読む固定 commit。作業ツリーの未コミット変更は読まない。
    openmontage_commit: str = "2fa571e39ad0632148dad77c7a2134f7e6fe0797"
    storyboard_timeout_seconds: int = 900

    # --- production（ADR-0017） ---
    #: 有料の画像・動画 provider の鍵。**secret**。未設定なら該当 worker を組めない。
    #: ``SecretStr``: repr / model_dump / ログに値を出さない。使う箇所で ``get_secret_value()``。
    fal_key: SecretStr | None = None
    #: task queue ごとの並行 Activity 数（worker が max_concurrent_activities に使う）
    image_concurrency: int = DEFAULT_IMAGE_CONCURRENCY
    voice_concurrency: int = DEFAULT_VOICE_CONCURRENCY
    video_concurrency: int = DEFAULT_VIDEO_CONCURRENCY
    #: ローカル TTS の音声モデル。未設定なら voice worker を組めない。
    piper_voice_path: str | None = None
    #: Piper を入れた**隔離 venv** の python（scripts/setup-piper.sh）。
    #: piper-tts は GPL-3.0 なので共有 venv に入れない。
    piper_python: str | None = None
    #: 合成パラメータ。未設定なら音声モデルの .onnx.json の既定値を使う。
    piper_length_scale: float | None = None
    piper_noise_scale: float | None = None
    piper_noise_w_scale: float | None = None
    production_submit_timeout_seconds: int = 120
    #: await の poll 期限。Activity の start_to_close
    #: （``AWAIT_START_TO_CLOSE_SECONDS``）より**短く**
    #: 取り、Temporal に殺される前に ``ProviderPollDeadlineError`` を返す（参照は台帳に残る）
    production_await_timeout_seconds: int = 35 * 60
    production_await_heartbeat_seconds: int = 90
    #: fal API の1回の読み取り待ちの上限。heartbeat timeout（90秒）より十分短くする
    production_fal_read_timeout_seconds: int = 30
    production_poll_interval_seconds: int = 10
    production_voice_timeout_seconds: int = 300
    #: ProductionWorkflow の1実行あたりの submit 試行予算（台帳のラウンド番号ではない）
    production_image_max_rounds: int = DEFAULT_IMAGE_MAX_ROUNDS
    production_video_max_rounds: int = DEFAULT_VIDEO_MAX_ROUNDS
    #: 状態不明の await 失敗に対し、同じ予約で await を追加実行する回数
    production_await_reexecutions: int = DEFAULT_AWAIT_REEXECUTIONS

    def __repr__(self) -> str:  # pragma: no cover - 事故防止のための表示抑制
        return "Settings(<redacted>)"
