"""0019 は expand-only で、旧コードに影響しない。語彙・期限の literal が契約と一致する（ADR-0081 / INV-132）。"""  # noqa: E501

from __future__ import annotations

import ast
import importlib.util
import pathlib
import re

from contracts.approval import ApproverKind
from contracts.quality import MAX_REPORT_VALIDITY
from contracts.quality_records import CheckKey, UsageFeature
from contracts.verdict import Verdict
from domain import release_preflight as rp

REPO = pathlib.Path(__file__).resolve().parents[2]
MIGRATION = REPO / "infrastructure/db/migrations/versions/0019_quality_check_records.py"
ALLOWED_OPS = {"create_table", "create_index", "execute", "get_bind"}
NEW_TABLES = {
    "quality_check_records",
    "quality_usage_declarations",
    "quality_reports",
    "quality_manual_approvals",
}


def _load():
    spec = importlib.util.spec_from_file_location("migration_0019", MIGRATION)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _upgrade_source() -> str:
    tree = ast.parse(MIGRATION.read_text(encoding="utf-8"))
    node = next(n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name == "upgrade")
    return ast.unparse(node)


def test_release_preflight_scanner_finds_only_the_truncate_guard_triggers_in_0019() -> None:
    """走査器は TRUNCATE の語を破壊的と数える。該当は「TRUNCATE を拒否するトリガ」の作成文だけで、
    ADR-0081（Accepted・0019 に言及）の marker で承認されている。他の文は 1 つも拾われない。"""
    text = MIGRATION.read_text(encoding="utf-8")
    findings = rp.scan_migration(text)
    assert {f.kind for f in findings} <= {"destructive"}
    lines = text.splitlines()
    for f in findings:
        number = int(f.text.rsplit("line ", 1)[1].rstrip(")"))
        statement = " ".join(lines[number - 1 : number + 2])
        assert "BEFORE TRUNCATE ON" in statement, f
    assert rp.contract_marker_numbers(text) == ["0081"]
    adr = (REPO / "docs/decisions/0081-quality-check-records-and-required-set.md").read_text(
        encoding="utf-8"
    )
    assert rp.adr_approves_migration(adr, "0019")


def test_0019_follows_0018_and_upgrade_only_adds() -> None:
    module = _load()
    assert (module.revision, module.down_revision) == ("0019", "0018")
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


def test_0019_creates_only_its_own_new_tables_and_touches_no_existing_one() -> None:
    source = _upgrade_source()
    created = set(re.findall(r"create_table\((\w+),", source))
    module = _load()
    assert {getattr(module, name) for name in created} == NEW_TABLES
    # 既存の表を ALTER しない（新しい表への CHECK 追加だけが ALTER を使う）
    altered = set(re.findall(r"ALTER TABLE (\w+)", MIGRATION.read_text(encoding="utf-8")))
    assert altered <= NEW_TABLES


def test_the_frozen_vocabulary_matches_the_contracts() -> None:
    module = _load()
    assert set(module.CHECK_KEYS) == {k.value for k in CheckKey}
    assert set(module.USAGE_FEATURES) == {f.value for f in UsageFeature}
    assert set(module.VERDICTS) == {v.value for v in Verdict}
    assert set(module.APPROVER_KINDS) == {k.value for k in ApproverKind}


def test_the_database_validity_limit_equals_the_contract() -> None:
    hours = int(MAX_REPORT_VALIDITY.total_seconds() // 3600)
    assert MAX_REPORT_VALIDITY.total_seconds() == hours * 3600
    assert f"interval '{hours} hours'" in MIGRATION.read_text(encoding="utf-8")


def test_every_table_gets_the_append_only_guard() -> None:
    module = _load()
    assert set(module.TABLES) == NEW_TABLES
    text = MIGRATION.read_text(encoding="utf-8")
    for table in NEW_TABLES:
        assert re.search(rf"BEFORE UPDATE ON {table} ", text) or f"BEFORE UPDATE ON {table}" in text


def test_every_table_rejects_delete_and_truncate_too() -> None:
    """追記専用は UPDATE だけでなく DELETE / TRUNCATE も拒否する（ADR-0086 §6 / INV-126）。"""
    module = _load()
    source = MIGRATION.read_text(encoding="utf-8")
    flat = re.sub(r'"\s*\n\s*"', "", source)
    for table in NEW_TABLES:
        assert f"CREATE TRIGGER {table}_no_delete BEFORE DELETE ON {table} " in flat, table
        assert f"CREATE TRIGGER {table}_no_truncate BEFORE TRUNCATE ON {table} " in flat, table
    # downgrade は同じトリガを外す（空の表なら戻せる）
    downgrade = source.split("def downgrade", 1)[1]
    assert "_no_delete" in downgrade and "_no_truncate" in downgrade
    assert set(module.TABLES) == NEW_TABLES


def test_episode_foreign_keys_restrict_and_the_approval_fk_includes_the_episode() -> None:
    """episode の削除で監査記録を巻き込まない / 別 episode の報告を承認できない。"""
    source = MIGRATION.read_text(encoding="utf-8")
    assert 'ondelete="RESTRICT"' in source and 'ondelete="CASCADE"' not in source
    assert '["report_id", "episode_id", "final_video_sha256"]' in source
    assert "uq_quality_reports_id_episode_sha" in source
