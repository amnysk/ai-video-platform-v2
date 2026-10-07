#!/usr/bin/env python
"""DB（正本）とオブジェクトストアの整合を検査する（読み取りのみ。設計 §14・§17-11）。

    python scripts/consistency-check.py               # 媒体本体（JSON の中の object_key）も検査
    python scripts/consistency-check.py --skip-media  # Artifact の JSON と agent_runs の参照だけ

接続先は Settings（``DATABASE_URL``・``MINIO_*``）。バックアップの復元先を検査するときは、復元先を
指す env で走らせる（``docs/operations/intervention-runbook.md`` §5）。何も書かない・直さない。

出力: 問題1件につき1行（``kind<TAB>table<TAB>row_id<TAB>detail``）と最後に集計1行。
終了コード: 0 = 不整合なし、1 = 不整合あり、2 = 引数の誤り。
"""

from __future__ import annotations

import argparse
import asyncio
import pathlib
import sys
from typing import Any

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

from infrastructure.intervention.consistency import check_consistency  # noqa: E402


def parse_args(argv: list[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=(__doc__ or "").splitlines()[0])
    parser.add_argument("--skip-media", action="store_true", help="JSON の中の媒体参照を検査しない")
    return parser.parse_args(argv)


async def main(
    argv: list[str] | None = None, *, session_factory: Any = None, store: Any = None
) -> int:
    args = parse_args(argv)
    if session_factory is None or store is None:
        from infrastructure.config import Settings
        from infrastructure.db.session import session_factory_from_settings
        from infrastructure.storage.minio_store import MinioArtifactStore

        settings = Settings()
        session_factory = session_factory or session_factory_from_settings(settings)
        store = store or MinioArtifactStore.from_settings(settings)
    report = await check_consistency(session_factory, store, check_media=not args.skip_media)
    for problem in report.problems:
        print(problem.line())
    print(
        f"consistency: {'OK' if report.ok else 'NG'} problems={len(report.problems)} "
        f"objects={report.checked_objects} refs={report.checked_refs}"
    )
    return 0 if report.ok else 1


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
