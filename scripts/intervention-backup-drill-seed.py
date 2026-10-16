#!/usr/bin/env python
"""復元演習の元データを作る（``scripts/intervention-backup-drill.sh`` が隔離スタックの中で呼ぶ）。

接続先は Settings（``DATABASE_URL``・``MINIO_*``）。Episode 1本ぶんの Artifact（媒体つき）と、
判断・介入の全ての表の行を足す。**隔離スタック（``avp2-oslog-c*``）専用**: 本番に向けない
（``AVP_ENVIRONMENT=test`` でなければ拒否する）。
"""

from __future__ import annotations

import asyncio
import os
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))


async def main() -> int:
    if os.environ.get("AVP_ENVIRONMENT") != "test":
        print("NG: AVP_ENVIRONMENT=test の隔離スタックでだけ実行する", file=sys.stderr)
        return 2
    from infrastructure.config import Settings
    from infrastructure.db.session import session_factory_from_settings
    from infrastructure.storage.minio_store import MinioArtifactStore
    from tests.support.intervention_world import seed_world

    settings = Settings()
    store = MinioArtifactStore.from_settings(settings)
    await store.ensure_bucket()
    world = await seed_world(session_factory_from_settings(settings), store)
    print(f"seeded episode={world.seed.episode_id} manifest={world.manifest_id}")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
