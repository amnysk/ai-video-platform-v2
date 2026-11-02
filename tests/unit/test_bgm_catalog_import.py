"""所有者向けの取込 CLI（ADR-0087 §4）。偽の repo / store で、本番には触れない。"""

from __future__ import annotations

import hashlib
import importlib.util
import json
import sys
from pathlib import Path

import pytest

from infrastructure.db.bgm_repositories import SqlBgmCatalogRepository
from tests.support.bgm_catalog import NOW, make_track

SCRIPT = Path(__file__).resolve().parents[2] / "scripts" / "bgm_catalog_import.py"
spec = importlib.util.spec_from_file_location("bgm_catalog_import_cli", SCRIPT)
assert spec and spec.loader
tool = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = tool
spec.loader.exec_module(tool)

BODY = b"fake-audio"
SHA = hashlib.sha256(BODY).hexdigest()


class FakeStore:
    def __init__(self) -> None:
        self.objects: dict[str, bytes] = {}

    async def put_bytes(self, key: str, data: bytes, content_type: str) -> None:
        self.objects[key] = data

    async def sha256_of(self, key: str) -> str:
        return hashlib.sha256(self.objects[key]).hexdigest()


def _catalog(tmp_path: Path) -> tuple[Path, Path]:
    originals = tmp_path / "orig"
    originals.mkdir()
    (originals / "ck61.flac").write_bytes(BODY)
    # 入力が「承認済み」を主張していても、取込は隔離にする
    track = make_track("bgm-ck61", "a", file_sha256=SHA, loudness_lufs=-14.5)
    catalog = tmp_path / "catalog.json"
    catalog.write_text(json.dumps({"tracks": [track.model_dump(mode="json")]}))
    return catalog, originals


async def test_dry_run_is_the_default_and_touches_nothing(tmp_path) -> None:
    catalog, originals = _catalog(tmp_path)
    store = FakeStore()
    report = await tool.import_tracks(
        None, store, tool.load_tracks(catalog), originals, apply=False, now=NOW
    )
    assert report == [{"track_id": "bgm-ck61", "result": "would_register_quarantined"}]
    assert store.objects == {}
    args = tool.build_parser().parse_args(["--catalog", "c", "--originals-dir", "o"])
    assert not args.apply and args.database_url is None


async def test_apply_registers_quarantined_then_owner_approval_is_a_separate_step(
    tmp_path, session_factory
) -> None:
    catalog, originals = _catalog(tmp_path)
    repo, store = SqlBgmCatalogRepository(session_factory), FakeStore()
    report = await tool.import_tracks(
        repo, store, tool.load_tracks(catalog), originals, apply=True, now=NOW
    )
    assert report[0]["result"] == "registered"
    assert f"music/originals/{SHA}.flac" in store.objects
    (entry,) = await repo.list_entries()
    assert entry.track.status == "quarantined" and entry.track.license_status == "unverified"

    again = await tool.import_tracks(
        repo, store, tool.load_tracks(catalog), originals, apply=True, now=NOW
    )
    assert again[0]["result"] == "duplicate"

    track = await tool.approve_as_owner(
        repo, "bgm-ck61", evidence_sha256="e" * 64, mix_gain_offset_db=6, now=NOW
    )
    assert track.status == "active" and track.license_approver_kind == "owner"
    assert track.mix_gain_offset_db == 6 and track.license_evidence_sha256 == "e" * 64


async def test_a_missing_original_is_reported_and_not_registered(tmp_path, session_factory) -> None:
    catalog, originals = _catalog(tmp_path)
    (originals / "ck61.flac").unlink()
    repo = SqlBgmCatalogRepository(session_factory)
    report = await tool.import_tracks(
        repo, FakeStore(), tool.load_tracks(catalog), originals, apply=True, now=NOW
    )
    assert report[0]["result"] == "original_missing" and await repo.list_entries() == ()


def test_writing_without_a_database_url_and_yes_is_refused(tmp_path, capsys) -> None:
    for argv in (["--apply"], ["--apply", "--database-url", "postgresql+asyncpg://x/y"]):
        assert tool.main(argv) == 2
    assert tool.main(["--approve-as-owner", "bgm-x", "--evidence-sha256", "e" * 64]) == 2
    assert "refusing" in capsys.readouterr().out


def test_the_tool_never_imports_fal_or_production() -> None:
    text = SCRIPT.read_text(encoding="utf-8")
    assert "infrastructure.providers" not in text and "production" not in text
    assert pytest is not None
