"""承認済み BGM の保存と再利用（ADR-0083 §2、INV-133）。

``LocalDirectoryBgmSource`` が読む配置（``${ai_video_work_root}/bgm/``）に**書く**側:

  catalog.json                       {"tracks": [BgmTrack, ...]}（track_id 昇順）
  music/originals/<sha256>.<ext>     原本。ファイル名が内容の sha256（content-addressed、不変）
  cues/<episode_id>.json             MusicCueSheet

- 同じ内容（sha256）の曲は新規登録せず既存を返す（Episode をまたいで再取込しない）
- 同じ内容のファイルが既にあれば書き直さない。違う内容が同じ名前にあれば変造として止める
- 登録できる曲の判定は ``domain.music.ingest_check``（純粋）。ここは I/O だけ
- ライセンスの承認（``license_status=approved`` と証拠）は人が行い、ここは運ぶだけ。
  承認を変えるのは別の操作で、登録（再取込）では既存の承認事実は変わらない（INV-99）
"""

from __future__ import annotations

import hashlib
import json
import os
import tempfile
from collections.abc import Iterable
from pathlib import Path

from contracts.music import BgmTrack, MusicCueSheet
from domain.music.ingest import IngestDecision, IngestOutcome, ingest_check
from infrastructure.render.bgm_source import CATALOG_FILE_NAME, CUES_DIR_NAME


class CatalogStoreError(Exception):
    """保存先が壊れている・変造されている。呼び出し側（管理者の操作）が止める。"""


def _atomic_write(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.")
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp, path)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise


def read_catalog(root: Path) -> tuple[BgmTrack, ...]:
    path = root / CATALOG_FILE_NAME
    if not path.is_file():
        return ()
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
        return tuple(BgmTrack.model_validate(t) for t in raw["tracks"])
    except (OSError, ValueError, KeyError, TypeError) as exc:
        raise CatalogStoreError(f"catalog unreadable: {type(exc).__name__}") from exc


def _write_catalog(root: Path, tracks: Iterable[BgmTrack]) -> None:
    ordered = sorted(tracks, key=lambda t: t.track_id)
    payload = {"tracks": [t.model_dump(mode="json") for t in ordered]}
    _atomic_write(
        root / CATALOG_FILE_NAME,
        (json.dumps(payload, ensure_ascii=False, sort_keys=True, indent=1) + "\n").encode(),
    )


def register_track(root: Path, track: BgmTrack, audio: bytes) -> IngestDecision:
    """曲を登録する。``ACCEPT`` のときだけ原本とカタログを書く。

    ``DUPLICATE`` は何も書かない（既存の曲・承認事実をそのまま使う）。``REJECT`` は何も書かない。
    """
    actual = hashlib.sha256(audio).hexdigest()
    existing = read_catalog(root)
    decision = ingest_check(existing, track, actual)
    if decision.outcome is not IngestOutcome.ACCEPT:
        return decision
    original = root / track.object_key
    if original.exists():
        if hashlib.sha256(original.read_bytes()).hexdigest() != actual:
            raise CatalogStoreError(f"original at {track.object_key} differs from its name")
    else:
        _atomic_write(original, audio)
    _write_catalog(root, (*existing, track))
    return decision


def write_cue_sheet(root: Path, episode_id: str, sheet: MusicCueSheet) -> Path:
    path = root / CUES_DIR_NAME / f"{episode_id}.json"
    _atomic_write(path, (sheet.model_dump_json(indent=1) + "\n").encode())
    return path


__all__ = [
    "CatalogStoreError",
    "read_catalog",
    "register_track",
    "write_cue_sheet",
]
