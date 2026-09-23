"""実行時設定。secretはここから外へ出さない（INV-20）。"""

from __future__ import annotations

from pydantic import SecretStr, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

from contracts.pipeline import (
    DAILY_SCHEDULE_ID,
    DEFAULT_DAILY_EPISODE_LIMIT,
    DEFAULT_DAILY_SCHEDULE_CRON,
    DEFAULT_SCHEDULE_TIMEZONE,
)
from contracts.production_activities import (
    DEFAULT_AWAIT_REEXECUTIONS,
    DEFAULT_IMAGE_CONCURRENCY,
    DEFAULT_IMAGE_MAX_ROUNDS,
    DEFAULT_VIDEO_CONCURRENCY,
    DEFAULT_VIDEO_MAX_ROUNDS,
    DEFAULT_VOICE_CONCURRENCY,
)
from contracts.render import (
    DEFAULT_RENDER_CONCURRENCY,
    DEFAULT_RENDER_FFMPEG_THREADS,
    DEFAULT_RENDER_MIN_FREE_BYTES,
    DEFAULT_RENDER_PROFILE_ID,
    DEFAULT_RENDER_TIMEOUT_SECONDS,
)
from contracts.schedule_guard import (
    DEFAULT_BLOCKED_GRACE_MINUTES,
    DEFAULT_COMPLETION_DEADLINE_HOURS,
    DEFAULT_STAGE_STALL_GRACE_MINUTES,
    DEFAULT_UPLOAD_DEADLINE_HOURS,
    DEFAULT_WATCHDOG_CRON,
    DEFAULT_WATCHDOG_GRACE_SECONDS,
)
from contracts.topic_planning import (
    CONTENT_PROFILES,
    DEFAULT_CONTENT_PROFILE_ID,
    DEFAULT_STRATEGY_PROFILE_ID,
    STRATEGY_PROFILES,
)
from contracts.upload import DEFAULT_UPLOAD_CHUNK_BYTES


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

    # --- render（Phase 5） ---
    #: 固定版 static ffmpeg の絶対パスと sha256（scripts/install-render-ffmpeg.sh が表示する）。
    #: 未設定・不一致なら render worker を組めない（起動前に検証する）。
    render_ffmpeg_path: str | None = None
    render_ffmpeg_sha256: str | None = None
    #: ffprobe は任意（プラットフォームの検査は PyAV で行う）。
    render_ffprobe_path: str | None = None
    render_ffprobe_sha256: str | None = None
    #: 字幕フォント。sha256 は render の input_hash に入る。
    render_font_path: str = "/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc"
    render_font_sha256: str = "b76b0433203017ca80401b2ee0dd69350349871c4b19d504c34dbdd80541690a"
    render_ffmpeg_threads: int = DEFAULT_RENDER_FFMPEG_THREADS
    #: render task queue の並行 Activity 数
    render_concurrency: int = DEFAULT_RENDER_CONCURRENCY
    render_timeout_seconds: int = DEFAULT_RENDER_TIMEOUT_SECONDS
    #: 作業領域に最低限残す空き容量（バイト）。見込み使用量はこれに上乗せする。
    render_min_free_bytes: int = DEFAULT_RENDER_MIN_FREE_BYTES

    # --- upload（Phase 6） ---
    #: OAuth installed app の client。secret は ``SecretStr``。
    youtube_client_id: str | None = None
    youtube_client_secret: SecretStr | None = None
    #: refresh token のファイル（repo 外・0600）。``scripts/youtube-oauth.py`` が書く。
    youtube_refresh_token_path: str | None = None
    #: 投稿先 channel の識別子（冪等キーの destination に入る）
    youtube_channel_id: str | None = None
    #: ``UPLOADS_PAUSED=true`` なら session を開始する前に止める
    uploads_paused: bool = False
    #: resumable upload の chunk（256 KiB の倍数）
    youtube_chunk_bytes: int = DEFAULT_UPLOAD_CHUNK_BYTES

    # --- pipeline（ADR-0023） ---
    #: 1 日（``schedule_timezone`` の日付）に自動生成する Episode の上限
    daily_episode_limit: int = DEFAULT_DAILY_EPISODE_LIMIT
    #: Temporal Schedule の cron（``schedule_timezone`` で解釈する）
    daily_schedule_cron: str = DEFAULT_DAILY_SCHEDULE_CRON
    schedule_timezone: str = DEFAULT_SCHEDULE_TIMEZONE
    daily_schedule_id: str = DAILY_SCHEDULE_ID
    #: daily watchdog（ADR-0027）。別の Schedule で毎時。猶予は予定時刻からの秒数
    watchdog_cron: str = DEFAULT_WATCHDOG_CRON
    watchdog_grace_seconds: int = DEFAULT_WATCHDOG_GRACE_SECONDS
    #: Episode 進行・完成・投稿の監視（ADR-0031）。工程・尺に固有の値をここ以外に埋め込まない
    stage_stall_grace_minutes: int = DEFAULT_STAGE_STALL_GRACE_MINUTES
    blocked_grace_minutes: int = DEFAULT_BLOCKED_GRACE_MINUTES
    completion_deadline_hours: float = DEFAULT_COMPLETION_DEADLINE_HOURS
    upload_deadline_hours: float = DEFAULT_UPLOAD_DEADLINE_HOURS
    #: 自動 pipeline が Render に渡す出力 profile（Shorts 前提にしない）
    pipeline_render_profile_id: str = DEFAULT_RENDER_PROFILE_ID
    #: ``PAUSED=true`` なら Daily の起動と投稿ゲートを止める（DB の switch と OR）
    paused: bool = False

    # --- Topic Planner（ADR-0025） ---
    #: profile の **id** だけを選ぶ。中身は ``contracts.topic_planning`` が唯一の宣言元
    topic_strategy_profile_id: str = DEFAULT_STRATEGY_PROFILE_ID
    topic_content_profile_id: str = DEFAULT_CONTENT_PROFILE_ID
    #: True なら planning worker が YouTube Analytics を live で取る（YOUTUBE_* の OAuth を使う）。
    #: False なら保存済み snapshot があればそれ（stale）、無ければ Analytics 無しで企画する
    youtube_analytics_enabled: bool = False

    @field_validator("topic_strategy_profile_id")
    @classmethod
    def _known_strategy(cls, v: str) -> str:
        if v not in STRATEGY_PROFILES:
            raise ValueError(f"unknown strategy profile {v!r}; known: {sorted(STRATEGY_PROFILES)}")
        return v

    @field_validator("topic_content_profile_id")
    @classmethod
    def _known_content(cls, v: str) -> str:
        if v not in CONTENT_PROFILES:
            raise ValueError(f"unknown content profile {v!r}; known: {sorted(CONTENT_PROFILES)}")
        return v

    def __repr__(self) -> str:  # pragma: no cover - 事故防止のための表示抑制
        return "Settings(<redacted>)"
