"""実行時設定。secretはここから外へ出さない（INV-20）。"""

from __future__ import annotations

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    database_url: str = "postgresql+asyncpg://avp:change-me@localhost:5432/avp"

    minio_endpoint: str = "http://localhost:9000"
    minio_access_key: str = "minioadmin"
    minio_secret_key: str = "change-me"
    minio_bucket: str = "artifacts"

    temporal_address: str = "localhost:7233"
    temporal_namespace: str = "default"
    temporal_task_queue: str = "episode-skeleton"

    def __repr__(self) -> str:  # pragma: no cover - 事故防止のための表示抑制
        return "Settings(<redacted>)"
