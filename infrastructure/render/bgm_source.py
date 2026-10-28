"""``BgmSource`` の暫定実装: ローカルディレクトリの JSON + 音源ファイル（DDL 無し。ADR-0077 §1）。

配置（``root`` = ``${ai_video_work_root}/bgm``）:
  catalog.json            {"tracks": [BgmTrack, ...]}
  cues/<episode_id>.json  MusicCueSheet
  <BgmTrack.object_key>   音源（root の外へ出るパスは拒否）

cue sheet のファイルが無い Episode は BGM 無し（catalog も読まない）。読めない・壊れている場合は
``BgmSourceError``（呼び出し側は BGM を使わず描画を続ける）。
"""

from __future__ import annotations

import json
from pathlib import Path

from pydantic import ValidationError

from contracts.artifact_refs import check_canonical_uuid
from contracts.music import BgmTrack, MusicCueSheet
from domain.render.ports import BgmSourceData, BgmSourceError
from infrastructure.render.binary import file_sha256

CATALOG_FILE_NAME = "catalog.json"
CUES_DIR_NAME = "cues"


class LocalDirectoryBgmSource:
    def __init__(self, root: str | Path) -> None:
        self._root = Path(root)

    def load(self, episode_id: str) -> BgmSourceData:
        try:
            check_canonical_uuid(episode_id)
        except ValueError as exc:
            raise BgmSourceError(f"invalid episode id: {episode_id!r}") from exc
        cue_path = self._root / CUES_DIR_NAME / f"{episode_id}.json"
        if not cue_path.is_file():
            return BgmSourceData(cue_sheet=None)
        try:
            sheet = MusicCueSheet.model_validate_json(cue_path.read_text(encoding="utf-8"))
            raw = json.loads((self._root / CATALOG_FILE_NAME).read_text(encoding="utf-8"))
            catalog = tuple(BgmTrack.model_validate(t) for t in raw["tracks"])
        except (OSError, ValueError, KeyError, TypeError, ValidationError) as exc:
            raise BgmSourceError(f"bgm source unreadable: {type(exc).__name__}") from exc
        by_id = {t.track_id: t for t in catalog}
        root = self._root.resolve()
        paths: dict[str, Path] = {}
        shas: dict[str, str | None] = {}
        for cue in sheet.cues:
            track = by_id.get(cue.track_id)
            if track is None or cue.track_id in shas:
                continue
            path = (root / track.object_key).resolve()
            shas[cue.track_id] = None
            if root not in path.parents or not path.is_file():
                continue
            paths[cue.track_id] = path
            try:
                shas[cue.track_id] = file_sha256(path)
            except OSError:
                shas[cue.track_id] = None
        return BgmSourceData(
            cue_sheet=sheet, catalog=catalog, track_paths=paths, file_sha256_by_track=shas
        )


__all__ = ["CATALOG_FILE_NAME", "CUES_DIR_NAME", "LocalDirectoryBgmSource"]
