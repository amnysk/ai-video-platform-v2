"""所有者向け: 手で置いた BGM を DB カタログ + MinIO へ移す（ADR-0087 §4）。

- 既定は ``--dry-run``（何も書かず、何を登録するかだけ表示する）。DB に書くには ``--apply`` と
  ``--database-url`` と ``--yes`` の全てが要る。本番に向けて実行するのは所有者の判断
- 取り込んだ曲は**常に隔離**（``quarantined`` / ``unverified``）。承認は持ち越さない
- 承認は別の明示操作 ``--approve-as-owner <track_id> --evidence-sha256 <hex>`` で、
  ``--mix-gain-offset-db`` で試聴で決めた持ち上げ幅（CK61 は 6）を同時に記録する
- 原本は ``music/originals/<sha256>.<ext>`` へ置き、読み戻して sha256 を照合する
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from collections.abc import Sequence
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from contracts.music import BgmTrack
from domain.music.ports import BgmCatalogRepository
from infrastructure.render.binary import file_sha256


def load_tracks(catalog: Path) -> tuple[BgmTrack, ...]:
    raw = json.loads(catalog.read_text(encoding="utf-8"))
    return tuple(BgmTrack.model_validate(t) for t in raw["tracks"])


def find_original(track: BgmTrack, originals_dir: Path) -> Path | None:
    """原本を探す。object_key の相対パス、無ければ sha256 が一致するファイル。"""
    direct = originals_dir / track.object_key
    if direct.is_file() and file_sha256(direct) == track.file_sha256:
        return direct
    for path in sorted(originals_dir.rglob("*")):
        if path.is_file() and file_sha256(path) == track.file_sha256:
            return path
    return None


def quarantined_copy(track: BgmTrack, original: Path) -> BgmTrack:
    """どんな入力でも隔離・未検証の形にする（承認を持ち越さない）。"""
    return track.model_copy(
        update={
            "object_key": f"music/originals/{track.file_sha256}{original.suffix}",
            "status": "quarantined",
            "license_status": "unverified",
            "license_checked_at": None,
            "license_approver_kind": None,
            "license_policy_version": None,
        }
    )


async def import_tracks(
    repo: BgmCatalogRepository | None,
    store: Any | None,
    tracks: Sequence[BgmTrack],
    originals_dir: Path,
    *,
    apply: bool,
    now: datetime,
) -> list[dict[str, Any]]:
    """各曲の結果。``apply`` が False なら repo / store は触らない。"""
    report: list[dict[str, Any]] = []
    for track in tracks:
        original = find_original(track, originals_dir)
        if original is None:
            report.append({"track_id": track.track_id, "result": "original_missing"})
            continue
        item = quarantined_copy(track, original)
        if not apply:
            report.append({"track_id": track.track_id, "result": "would_register_quarantined"})
            continue
        assert repo is not None and store is not None
        await store.put_bytes(item.object_key, original.read_bytes(), "application/octet-stream")
        if await store.sha256_of(item.object_key) != item.file_sha256:
            report.append({"track_id": track.track_id, "result": "readback_mismatch"})
            continue
        _, created = await repo.register_quarantined(
            item,
            acquired_from="owner_supplied",
            acquired_at=now,
            evidence_object_key=None,
            analysis={"loudness_lufs": track.loudness_lufs},
            inferred_tags={},
        )
        report.append(
            {"track_id": track.track_id, "result": "registered" if created else "duplicate"}
        )
    return report


async def approve_as_owner(
    repo: BgmCatalogRepository,
    track_id: str,
    *,
    evidence_sha256: str,
    mix_gain_offset_db: int,
    now: datetime,
) -> BgmTrack:
    """隔離中の曲を所有者の承認で有効にする（別の明示操作。規則の版は持たない）。"""
    entries = {e.track.track_id: e.track for e in await repo.list_entries()}
    track = entries.get(track_id)
    if track is None:
        raise SystemExit(f"unknown track {track_id!r}")
    approved = track.model_copy(
        update={
            "status": "active",
            "license_status": "approved",
            "license_evidence_sha256": evidence_sha256,
            "license_checked_at": now,
            "license_approver_kind": "owner",
            "license_policy_version": None,
            "mix_gain_offset_db": mix_gain_offset_db,
        }
    )
    approved = BgmTrack.model_validate(approved.model_dump())
    return (await repo.promote(approved, verified_at=now)).track


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--catalog", type=Path, help="catalog.json（{'tracks': [...]}）")
    p.add_argument("--originals-dir", type=Path, help="原本のあるディレクトリ")
    p.add_argument("--apply", action="store_true", help="DB と MinIO に書く（既定は dry-run）")
    p.add_argument("--database-url", help="書き込み先（--apply と --yes が要る）")
    p.add_argument("--yes", action="store_true", help="DB URL に対する実行を確認する")
    p.add_argument("--approve-as-owner", metavar="TRACK_ID")
    p.add_argument("--evidence-sha256")
    p.add_argument("--mix-gain-offset-db", type=int, default=0)
    return p


async def _run(args: argparse.Namespace) -> int:
    now = datetime.now(UTC)
    writing = args.apply or args.approve_as_owner
    if writing and not (args.database_url and args.yes):
        print("refusing: writing needs --database-url and --yes (dry-run is the default)")
        return 2
    repo = store = None
    if writing:
        from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

        from infrastructure.config import Settings
        from infrastructure.db.bgm_repositories import SqlBgmCatalogRepository
        from infrastructure.storage.minio_store import MinioArtifactStore

        engine = create_async_engine(args.database_url, hide_parameters=True)
        repo = SqlBgmCatalogRepository(async_sessionmaker(engine, expire_on_commit=False))
        store = MinioArtifactStore.from_settings(Settings())
    if args.approve_as_owner:
        if not args.evidence_sha256:
            print("--approve-as-owner needs --evidence-sha256")
            return 2
        assert repo is not None
        track = await approve_as_owner(
            repo,
            args.approve_as_owner,
            evidence_sha256=args.evidence_sha256,
            mix_gain_offset_db=args.mix_gain_offset_db,
            now=now,
        )
        print(json.dumps({"approved": track.track_id, "offset": track.mix_gain_offset_db}))
        return 0
    if not (args.catalog and args.originals_dir):
        print("--catalog and --originals-dir are required")
        return 2
    report = await import_tracks(
        repo, store, load_tracks(args.catalog), args.originals_dir, apply=bool(args.apply), now=now
    )
    print(json.dumps({"dry_run": not args.apply, "tracks": report}, ensure_ascii=False, indent=1))
    return 0


def main(argv: Sequence[str] | None = None) -> int:
    return asyncio.run(_run(build_parser().parse_args(argv)))


if __name__ == "__main__":
    sys.exit(main())
