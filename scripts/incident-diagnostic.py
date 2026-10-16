#!/usr/bin/env python
"""1 Episode の読み取り専用インシデント診断（2026-09-21/22 の fal 403 事故の復旧判断用）。

    python scripts/incident-diagnostic.py <episode_id>

ロジックは ``infrastructure/diagnostics/episode_diagnostic.py``（ユニットテスト対象。
``domain.pipeline.resume_plan.build_resume_plan`` と
``infrastructure.artifact.verify.verify_artifact`` を呼ぶだけで、再開・再利用の判定を
再実装しない）。このスクリプトは CLI の薄い皮と、
「現在有効な生成設定版」の解決（ADR-0033 §2-3: fal の固定定数はここでだけ import する ──
``infrastructure/artifact/verify.py`` には埋め込まない）だけを持つ。

**読み取り専用**: DB は SELECT だけ、MinIO は ``stat``/``get_json``/``sha256_of``/``exists`` だけを
呼ぶ。``session.commit()`` は一度も呼ばない。provider 呼び出し・予約 INSERT・workflow start は
一切行わない。fal・YouTube への実ネットワーク呼び出しは無い。
"""

from __future__ import annotations

import asyncio
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

from contracts.states import ArtifactType  # noqa: E402
from domain.production.prompting import DEFAULT_VIDEO_MOTION  # noqa: E402
from infrastructure.artifact.verify import ArtifactVerdict  # noqa: E402
from infrastructure.config import Settings  # noqa: E402
from infrastructure.db.session import session_factory_from_settings  # noqa: E402
from infrastructure.diagnostics.episode_diagnostic import (  # noqa: E402
    diagnose_episode,
    format_report,
)

# 「現在有効な生成設定版」── fal adapter からそのまま（ADR-0033 §2-3 と同じ値の作り方:
# video は generator + motion profile の合成。verify 側にこの定数を埋め込まない設計を保つため、
# この import は infrastructure/artifact/verify.py ではなくこのスクリプトだけが行う）。
from infrastructure.providers.fal_seedance_video import SEEDANCE_PROFILE_ID  # noqa: E402
from infrastructure.providers.fal_seedream_image import SEEDREAM_PROFILE_ID  # noqa: E402
from infrastructure.storage.minio_store import MinioArtifactStore  # noqa: E402

_CURRENT_VIDEO_PROFILE_ID = f"{SEEDANCE_PROFILE_ID}+{DEFAULT_VIDEO_MOTION.motion_profile_id}"


def _profile_for(artifact_type: ArtifactType) -> str | None:
    if artifact_type is ArtifactType.SCENE_IMAGE:
        return SEEDREAM_PROFILE_ID
    if artifact_type is ArtifactType.SCENE_VIDEO:
        return _CURRENT_VIDEO_PROFILE_ID
    return None


async def main(episode_id: str) -> int:
    settings = Settings()
    session_factory = session_factory_from_settings(settings)
    store = MinioArtifactStore.from_settings(settings)

    report = await diagnose_episode(episode_id, session_factory, store, profile_for=_profile_for)
    if report is None:
        print(f"NG: episode {episode_id} not found (read-only lookup).")
        return 1

    print(format_report(report))
    print()
    non_reusable = [
        row for row in report.artifact_verifications if row.verdict is not ArtifactVerdict.REUSABLE
    ]
    print(
        f"summary: {len(report.artifact_verifications)} artifact(s) verified, "
        f"{len(report.artifact_verifications) - len(non_reusable)} REUSABLE, "
        f"{len(non_reusable)} not-reusable, "
        f"{len(report.reservations)} reservation(s) on record."
    )
    return 0


if __name__ == "__main__":
    if len(sys.argv) != 2:
        print(f"usage: {sys.argv[0]} <episode_id>", file=sys.stderr)
        sys.exit(2)
    sys.exit(asyncio.run(main(sys.argv[1])))
