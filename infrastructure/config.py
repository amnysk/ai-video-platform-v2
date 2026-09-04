"""実行時設定。secretはここから外へ出さない（INV-20）。"""

from __future__ import annotations

from pydantic_settings import BaseSettings, SettingsConfigDict


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

    def __repr__(self) -> str:  # pragma: no cover - 事故防止のための表示抑制
        return "Settings(<redacted>)"
