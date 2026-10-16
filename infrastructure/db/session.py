"""DBエンジンとセッションファクトリ。"""

from __future__ import annotations

from functools import lru_cache

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from infrastructure.config import Settings


@lru_cache(maxsize=1)
def build_session_factory(database_url: str) -> async_sessionmaker[AsyncSession]:
    engine = create_async_engine(
        database_url,
        pool_pre_ping=True,
        # 例外文に SQL の引数（予約の session URI 等）を載せない（INV-20）
        hide_parameters=True,
    )
    return async_sessionmaker(engine, expire_on_commit=False)


def session_factory_from_settings(settings: Settings) -> async_sessionmaker[AsyncSession]:
    return build_session_factory(settings.database_url)
