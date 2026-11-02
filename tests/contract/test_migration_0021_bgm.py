"""0021 は expand-only で、旧コードに影響しない。BgmTrack の全列を持つ（ADR-0087 §4 / INV-153）。"""

from __future__ import annotations

import ast
import importlib.util
import pathlib
import re

from contracts.music import BgmTrack
from domain import release_preflight as rp
from infrastructure.db.models import Base

REPO = pathlib.Path(__file__).resolve().parents[2]
MIGRATION = REPO / "infrastructure/db/migrations/versions/0021_bgm_catalog.py"
ALLOWED_OPS = {"create_table", "create_index"}
NEW_TABLES = {
    "bgm_tracks",
    "bgm_selections",
    "bgm_acquisition_runs",
    "bgm_acquisition_demands",
}


def _load():
    spec = importlib.util.spec_from_file_location("migration_0021", MIGRATION)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_release_preflight_scanner_finds_nothing_in_0021() -> None:
    """素の expand-only として走査を通る（走査器は弱めない。marker に頼らない）。"""
    text = MIGRATION.read_text(encoding="utf-8")
    assert rp.scan_migration(text) == []
    assert rp.contract_marker_numbers(text) == []


def test_0021_follows_0019_and_upgrade_only_adds() -> None:
    module = _load()
    assert (module.revision, module.down_revision) == ("0021", "0019")
    tree = ast.parse(MIGRATION.read_text(encoding="utf-8"))
    upgrade = next(n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name == "upgrade")
    ops = {
        node.func.attr
        for node in ast.walk(upgrade)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and isinstance(node.func.value, ast.Name)
        and node.func.value.id == "op"
    }
    assert ops <= ALLOWED_OPS, ops - ALLOWED_OPS


def test_0021_creates_only_its_own_new_tables() -> None:
    module = _load()
    assert set(module.TABLES) == NEW_TABLES
    text = MIGRATION.read_text(encoding="utf-8")
    assert "ALTER TABLE" not in text
    assert set(re.findall(r"create_table\(\s*(\w+),", text)) == {
        "TRACKS",
        "SELECTIONS",
        "RUNS",
        "DEMANDS",
    }


def test_no_foreign_keys_so_each_table_can_be_dropped_alone() -> None:
    assert "ForeignKey" not in MIGRATION.read_text(encoding="utf-8")
    for name in NEW_TABLES:
        assert not Base.metadata.tables[name].foreign_keys, name


def test_bgm_tracks_has_every_bgm_track_field_and_the_adr_0087_columns() -> None:
    columns = {c.name for c in Base.metadata.tables["bgm_tracks"].columns}
    assert set(BgmTrack.model_fields) <= columns
    assert {
        "acquired_from",
        "acquired_at",
        "evidence_object_key",
        "analysis",
        "inferred_tags",
        "last_verified_at",
    } <= columns


def test_uniqueness_that_makes_selection_once_and_dedupe_enforceable() -> None:
    tracks = Base.metadata.tables["bgm_tracks"]
    selections = Base.metadata.tables["bgm_selections"]
    assert {c.name for c in tracks.primary_key.columns} == {"track_id"}
    text = MIGRATION.read_text(encoding="utf-8")
    assert 'UniqueConstraint("file_sha256", name="uq_bgm_tracks_file_sha256")' in text
    assert 'UniqueConstraint("episode_id", name="uq_bgm_selections_episode_id")' in text
    names = {c.name for c in selections.constraints}
    assert "uq_bgm_selections_episode_id" in names


def test_downgrade_refuses_to_drop_rows() -> None:
    source = MIGRATION.read_text(encoding="utf-8").split("def downgrade", 1)[1]
    assert "refusing to drop" in source and "SELECT count(*)" in source


def test_the_docstring_records_the_reserved_0020() -> None:
    assert "0020" in (_load().__doc__ or "")
