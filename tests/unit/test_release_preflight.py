"""release preflight の判定（ADR-0050 / INV-65）。collector は全て fake（本番に触れない）。

各テストが存在する理由は docs/testing/release-preflight-rationale.md。
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest

from contracts.ci import CI_GATE_JOB_NAME
from domain import release_preflight as rp
from domain.backup.manifest import REQUIRED_DRILL_CHECKS, drill_digest

SHA = "a" * 40
OLD = "b" * 40
NOW = datetime(2026, 10, 20, 12, 0, tzinfo=UTC)

SAFE_MIGRATION = '''"""add column"""
import sqlalchemy as sa
from alembic import op

revision = "0016"
down_revision = "0015"


def upgrade() -> None:
    op.add_column("t", sa.Column("x", sa.Text(), nullable=True))


def downgrade() -> None:
    op.drop_column("t", "x")
'''
BASE_MIGRATION = 'revision = "0015"\ndown_revision = "0014"\n\n\ndef upgrade() -> None:\n    pass\n'
ROOT_MIGRATION = 'revision = "0014"\ndown_revision = None\n\n\ndef upgrade() -> None:\n    pass\n'

ACCEPTED_ADR = (
    "# ADR-0050: contract\n\n## Status\n\nAccepted (2026-10-20)\n\nDrops t.x in revision 0016.\n"
)

SCHEDULES = {
    "avp-daily-episode": {"exists": True, "paused": False, "maintenance": False},
    "avp-daily-watchdog": {"exists": True, "paused": False, "maintenance": False},
}


def _manifest(age_hours: float = 1.0, drop: str | None = None, verified: bool = True) -> str:
    stamp = (NOW - timedelta(hours=age_hours)).isoformat()
    comps = {c: {"created_at": stamp} for c in rp.BACKUP_COMPONENTS if c != drop}
    manifest: dict[str, Any] = {"components": comps, "restore_verified": verified}
    if verified:
        drill: dict[str, Any] = {
            "result": "pass",
            "problems": [],
            "checks": {c: {"passed": True} for c in REQUIRED_DRILL_CHECKS},
        }
        drill["evidence_sha256"] = drill_digest(drill)
        manifest["restore_drill"] = drill
    return json.dumps(manifest)


def sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _reviewed_report(
    sha: str = SHA, mode: str = "pre-deploy", overall: str = "UNKNOWN", broken: str | None = None
) -> str:
    """owner が見て承認する preflight 出力。承認だけが UNKNOWN の状態（他は全て PASS）。"""
    names = [n for n in rp.PRE_DEPLOY_REQUIRED if n != "owner_approval"]
    results = [
        {
            "name": n,
            "status": "BLOCKED" if n == broken else "PASS",
            "detail": "",
            "data": {"schedules": SCHEDULES} if n == "schedule_snapshot" else {},
        }
        for n in names
    ]
    results.append({"name": "owner_approval", "status": "UNKNOWN", "detail": "", "data": {}})
    return json.dumps({"target_sha": sha, "mode": mode, "overall": overall, "results": results})


def _approval(
    sha: str = SHA,
    *,
    at: datetime = NOW,
    report: str | None = None,
    extra: dict[str, Any] | None = None,
) -> str:
    doc: dict[str, Any] = {
        "sha": sha,
        "approver": "owner",
        "approved_at": at.isoformat(),
        "report_sha256": sha256_text(_reviewed_report() if report is None else report),
    }
    doc.update(extra or {})
    return json.dumps(doc)


def _replay(sha: str = SHA, result: str = "pass") -> str:
    return json.dumps({"sha": sha, "result": result, "at": NOW.isoformat()})


@dataclass
class Fake:
    """既定は全項目が良好。テストが1項目ずつ壊す。値が Exception なら呼び出しで raise する。"""

    commit_ok: Any = True
    head: Any = field(default_factory=lambda: rp.HeadState(head_sha=SHA, dirty=False))
    revisions: Any = field(default_factory=lambda: {"api": OLD, "script-worker": OLD})
    relation: Any = rp.Relation.FAST_FORWARD
    ci: Any = field(default_factory=lambda: [rp.CheckRun(CI_GATE_JOB_NAME, "completed", "success")])
    alembic: Any = "0015"
    files: Any = field(
        default_factory=lambda: {
            OLD: {"0014.py": ROOT_MIGRATION, "0015.py": BASE_MIGRATION},
            SHA: {"0014.py": ROOT_MIGRATION, "0015.py": BASE_MIGRATION, "0016.py": SAFE_MIGRATION},
        }
    )
    adr_texts: Any = field(default_factory=lambda: {(OLD, "0050"): ACCEPTED_ADR})
    running: Any = 2
    pending: Any = 0
    unhealthy: Any = field(default_factory=list)
    schedules: Any = field(default_factory=lambda: dict(SCHEDULES))
    queues: Any = field(default_factory=lambda: ["pipeline", "render"])
    missing_queues: Any = field(default_factory=list)
    expected: Any = field(default_factory=lambda: ["api"])
    target: Any = field(
        default_factory=lambda: rp.TargetInfo(
            db_host="db.internal",
            db_name="avp",
            temporal_address="t:7233",
            temporal_namespace="default",
            environment="",
        )
    )
    texts: Any = field(
        default_factory=lambda: {
            "backup.json": _manifest(),
            "replay.json": _replay(),
            "approval.json": _approval(),
            "report.json": _reviewed_report(),
        }
    )

    def _get(self, value: Any) -> Any:
        if isinstance(value, Exception):
            raise value
        return value

    def commit_exists(self, sha: str) -> bool:
        return self._get(self.commit_ok)

    def head_state(self) -> rp.HeadState:
        return self._get(self.head)

    def container_revisions(self) -> dict[str, str | None]:
        return self._get(self.revisions)

    def ancestry(self, deployed: str, target: str) -> rp.Relation:
        return self._get(self.relation)

    def ci_check_runs(self, sha: str) -> list[rp.CheckRun]:
        return self._get(self.ci)

    def db_alembic_version(self) -> str | None:
        return self._get(self.alembic)

    def migration_files(self, rev: str) -> dict[str, str]:
        return self._get(self.files)[rev]

    def adr_text(self, rev: str, number: str) -> str | None:
        return self._get(self.adr_texts).get((rev, number))

    def running_workflow_count(self) -> int:
        return self._get(self.running)

    def pending_reservation_count(self) -> int:
        return self._get(self.pending)

    def unhealthy_services(self) -> list[str]:
        return self._get(self.unhealthy)

    def schedule_states(self) -> dict[str, dict[str, object]]:
        return self._get(self.schedules)

    def required_queues(self) -> list[str]:
        return self._get(self.queues)

    def queues_without_poller(self, queues: Sequence[str]) -> list[str]:
        return self._get(self.missing_queues)

    def expected_services(self) -> list[str]:
        return self._get(self.expected)

    def target_info(self) -> rp.TargetInfo:
        return self._get(self.target)

    def read_text(self, path: str) -> str | None:
        return self._get(self.texts).get(path)


def _request(**kw: Any) -> rp.Request:
    base: dict[str, Any] = {
        "target_sha": SHA,
        "backup_manifest": "backup.json",
        "replay_evidence": "replay.json",
        "approval_file": "approval.json",
        "approved_report": "report.json",
    }
    base.update(kw)
    return rp.Request(**base)


def _run(fake: Fake, **kw: Any) -> rp.Report:
    return rp.run_preflight(_request(**kw), fake, NOW)


def _status(report: rp.Report, name: str) -> rp.Status:
    return next(r.status for r in report.results if r.name == name)


# ---------------------------------------------------------------- 集約（INV-65 の核）


def test_all_pass_is_ready() -> None:
    report = _run(Fake())
    assert report.overall is rp.Overall.READY, [r for r in report.results if r.status != "PASS"]
    assert {r.name for r in report.results} >= set(rp.PRE_DEPLOY_REQUIRED)


def test_unknown_never_maps_to_ready() -> None:
    """どのチェックを UNKNOWN にしても、総合は READY にならない（INV-65）。"""
    for name in rp.PRE_DEPLOY_REQUIRED:
        results = [rp.CheckResult(n, rp.Status.PASS, "ok") for n in rp.PRE_DEPLOY_REQUIRED]
        results = [
            rp.CheckResult(r.name, rp.Status.UNKNOWN, "?") if r.name == name else r for r in results
        ]
        assert rp.aggregate(results, rp.PRE_DEPLOY_REQUIRED) is rp.Overall.UNKNOWN, name


def test_blocked_wins_over_unknown() -> None:
    results = [
        rp.CheckResult("a", rp.Status.UNKNOWN, "?"),
        rp.CheckResult("b", rp.Status.BLOCKED, "no"),
    ]
    assert rp.aggregate(results, ("a", "b")) is rp.Overall.BLOCKED


def test_missing_required_check_is_unknown_not_ready() -> None:
    results = [rp.CheckResult("a", rp.Status.PASS, "ok")]
    assert rp.aggregate(results, ("a", "b")) is rp.Overall.UNKNOWN
    assert rp.aggregate([], ("a",)) is rp.Overall.UNKNOWN


def test_exit_codes() -> None:
    assert rp.exit_code(rp.Overall.READY) == 0
    assert rp.exit_code(rp.Overall.BLOCKED) == 2
    assert rp.exit_code(rp.Overall.UNKNOWN) == 3


def test_collector_exception_becomes_unknown() -> None:
    """collector が何を投げても（想定外の例外でも）、READY にはならず UNKNOWN になる。"""
    report = _run(Fake(ci=RuntimeError("boom")))
    assert _status(report, "ci") is rp.Status.UNKNOWN
    assert report.overall is rp.Overall.UNKNOWN
    report = _run(Fake(alembic=rp.CollectorUnavailable("db down")))
    assert _status(report, "alembic") is rp.Status.UNKNOWN


# ---------------------------------------------------------------- 個別のチェック


@pytest.mark.parametrize("bad", ["", "abc1234", "A" * 40, "g" * 40, "a" * 41, "HEAD", "main"])
def test_target_sha_must_be_full_lowercase_hex(bad: str) -> None:
    report = _run(Fake(), target_sha=bad)
    assert _status(report, "target_sha") is rp.Status.BLOCKED
    assert report.overall is rp.Overall.BLOCKED


def test_target_sha_must_exist_locally() -> None:
    assert _status(_run(Fake(commit_ok=False)), "target_sha") is rp.Status.BLOCKED
    assert (
        _status(
            _run(
                Fake(commit_ok=OSError("git")),
            ),
            "target_sha",
        )
        is rp.Status.UNKNOWN
    )


def test_dependent_checks_do_not_run_on_an_invalid_sha() -> None:
    fake = Fake(ci=AssertionError("must not be called"))
    report = _run(fake, target_sha="main")
    assert _status(report, "ci") is rp.Status.UNKNOWN


def test_worktree_must_be_clean_and_at_the_sha() -> None:
    dirty = Fake(head=rp.HeadState(head_sha=SHA, dirty=True))
    assert _status(_run(dirty), "worktree_clean") is rp.Status.BLOCKED
    elsewhere = Fake(head=rp.HeadState(head_sha=OLD, dirty=False))
    assert _status(_run(elsewhere), "worktree_clean") is rp.Status.BLOCKED


@pytest.mark.parametrize(
    ("relation", "status"),
    [
        (rp.Relation.FAST_FORWARD, rp.Status.PASS),
        (rp.Relation.SAME, rp.Status.PASS),
        (rp.Relation.BEHIND, rp.Status.BLOCKED),
        (rp.Relation.DIVERGED, rp.Status.BLOCKED),
        (rp.Relation.UNKNOWN_COMMIT, rp.Status.UNKNOWN),
    ],
)
def test_ancestry_requires_fast_forward_from_production(
    relation: rp.Relation, status: rp.Status
) -> None:
    assert _status(_run(Fake(relation=relation)), "ancestry") is status


def test_non_fast_forward_needs_the_explicit_flag() -> None:
    fake = Fake(relation=rp.Relation.BEHIND)
    assert _status(_run(fake, allow_non_ff=True), "ancestry") is rp.Status.PASS


def test_ancestry_is_unknown_when_the_deployed_revision_is_not_determined() -> None:
    mixed = Fake(revisions={"api": OLD, "w": SHA})
    assert _status(_run(mixed), "ancestry") is rp.Status.UNKNOWN


def _gate(conclusion: str | None, status: str = "completed") -> rp.CheckRun:
    return rp.CheckRun(CI_GATE_JOB_NAME, status, conclusion)


@pytest.mark.parametrize(
    ("runs", "status"),
    [
        ([_gate("success")], "PASS"),
        # gate が success なら、他の check run の有無・結果は gate が既に集約している
        ([_gate("success"), rp.CheckRun("Ruff", "completed", "success")], "PASS"),
        ([_gate("failure")], "BLOCKED"),
        ([_gate("cancelled")], "BLOCKED"),
        ([_gate("failure"), _gate("success")], "BLOCKED"),
        ([_gate(None, "in_progress")], "UNKNOWN"),
        ([_gate("skipped")], "UNKNOWN"),
        ([_gate("neutral")], "UNKNOWN"),
        ([], "UNKNOWN"),
        # gate が無い（名前違い・他の check だけが全て success）は READY の根拠にならない
        ([rp.CheckRun("test", "completed", "success")], "UNKNOWN"),
        ([rp.CheckRun("CI gate (copy)", "completed", "success")], "UNKNOWN"),
        ([rp.CheckRun("ci gate", "completed", "success")], "UNKNOWN"),
        # gate が無くて他が失敗していても、gate の失敗ではないので UNKNOWN（確認できていない）
        ([rp.CheckRun("Ruff", "completed", "failure")], "UNKNOWN"),
    ],
)
def test_ci_requires_the_gate_check_run_to_have_succeeded(
    runs: list[rp.CheckRun], status: str
) -> None:
    assert _status(_run(Fake(ci=runs)), "ci").value == status


def test_ci_gate_name_comes_from_the_shared_contract() -> None:
    assert rp.CI_GATE_JOB_NAME == CI_GATE_JOB_NAME == "CI gate"


def test_ci_gate_failure_blocks_even_if_other_runs_are_still_going() -> None:
    runs = [rp.CheckRun("Ruff", "in_progress", None), _gate("failure")]
    assert _status(_run(Fake(ci=runs)), "ci") is rp.Status.BLOCKED


def test_container_revision_mismatch_is_blocked() -> None:
    mixed = Fake(revisions={"api": OLD, "w": "c" * 40})
    assert _status(_run(mixed), "container_revisions") is rp.Status.BLOCKED


@pytest.mark.parametrize(
    "revs", [{"api": OLD, "w": None}, {"api": OLD, "w": "unknown"}, {"api": OLD + "-dirty"}]
)
def test_container_without_a_clean_revision_is_blocked(revs: dict[str, str | None]) -> None:
    assert _status(_run(Fake(revisions=revs)), "container_revisions") is rp.Status.BLOCKED


def test_no_containers_or_unreachable_docker_is_unknown() -> None:
    assert _status(_run(Fake(revisions={})), "container_revisions") is rp.Status.UNKNOWN
    unreachable = Fake(revisions=rp.CollectorUnavailable("docker"))
    assert _status(_run(unreachable), "container_revisions") is rp.Status.UNKNOWN


def test_alembic_prod_must_be_on_the_target_chain() -> None:
    assert _status(_run(Fake(alembic="0014")), "alembic") is rp.Status.PASS
    assert _status(_run(Fake(alembic="0016")), "alembic") is rp.Status.PASS
    assert _status(_run(Fake(alembic="9999")), "alembic") is rp.Status.BLOCKED
    assert _status(_run(Fake(alembic=None)), "alembic") is rp.Status.UNKNOWN


# ---------------------------------------------------------------- migration の expand-only


def _with_new_migration(text: str, extra: str = "") -> Fake:
    return Fake(
        files={
            OLD: {"0014.py": ROOT_MIGRATION, "0015.py": BASE_MIGRATION},
            SHA: {"0014.py": ROOT_MIGRATION, "0015.py": BASE_MIGRATION, "0016.py": extra + text},
        }
    )


def _upgrade(body: str) -> str:
    return (
        'revision = "0016"\ndown_revision = "0015"\n'
        "import sqlalchemy as sa\nfrom alembic import op\n\n\n"
        f"def upgrade() -> None:\n    {body}\n\n\ndef downgrade() -> None:\n    pass\n"
    )


def test_expand_only_migration_passes_even_though_its_downgrade_drops() -> None:
    """downgrade の drop_column は数えない（upgrade だけを見る）。"""
    assert _status(_run(_with_new_migration(SAFE_MIGRATION)), "migration_compat") is rp.Status.PASS


def test_no_new_migration_passes() -> None:
    assert (
        _status(
            _run(Fake(files={OLD: {"a.py": BASE_MIGRATION}, SHA: {"a.py": BASE_MIGRATION}})),
            "migration_compat",
        )
        is rp.Status.PASS
    )


@pytest.mark.parametrize(
    "body",
    [
        'op.drop_column("t", "x")',
        'op.drop_table("t")',
        'op.rename_table("a", "b")',
        'op.alter_column("t", "x", new_column_name="y")',
        'op.alter_column("t", "x", nullable=False)',
        'op.add_column("t", sa.Column("x", sa.Text(), nullable=False))',
        'op.execute("ALTER TABLE t DROP COLUMN x")',
        'op.execute("alter table t rename to u")',
        'op.execute("TRUNCATE t")',
    ],
)
def test_destructive_migration_is_blocked_without_marker(body: str) -> None:
    fake = _with_new_migration(_upgrade(body))
    assert _status(_run(fake), "migration_compat") is rp.Status.BLOCKED


def test_not_null_with_server_default_is_expand() -> None:
    body = 'op.add_column("t", sa.Column("x", sa.Integer(), nullable=False, server_default="0"))'
    assert _status(_run(_with_new_migration(_upgrade(body))), "migration_compat") is rp.Status.PASS


def test_destructive_words_in_comments_and_downgrade_are_ignored() -> None:
    text = _upgrade(
        'op.add_column("t", sa.Column("x", sa.Text(), nullable=True))  # not drop_column'
    )
    assert _status(_run(_with_new_migration(text)), "migration_compat") is rp.Status.PASS


def test_contract_marker_with_an_accepted_deployed_adr_allows_it() -> None:
    marked = _upgrade('op.drop_column("t", "x")') + "\n# contract-migration: approved ADR-0050\n"
    assert _status(_run(_with_new_migration(marked)), "migration_compat") is rp.Status.PASS


def test_contract_marker_without_a_matching_adr_is_blocked() -> None:
    marked = _upgrade('op.drop_column("t", "x")') + "\n# contract-migration: approved ADR-0777\n"
    assert _status(_run(_with_new_migration(marked)), "migration_compat") is rp.Status.BLOCKED


def test_unparseable_migration_is_unknown() -> None:
    assert (
        _status(_run(_with_new_migration("def upgrade(:\n")), "migration_compat")
        is rp.Status.UNKNOWN
    )


def test_new_migration_is_judged_by_filename_against_the_deployed_revision() -> None:
    """既に本番にあるファイルの内容（過去の contract）は再判定しない。"""
    old_destructive = _upgrade('op.drop_column("t", "x")')
    fake = Fake(
        files={
            OLD: {"0014.py": ROOT_MIGRATION, "0015.py": old_destructive},
            SHA: {"0014.py": ROOT_MIGRATION, "0015.py": old_destructive},
        }
    )
    assert _status(_run(fake), "migration_compat") is rp.Status.PASS


# ---------------------------------------------------------------- 実行中・予約・Schedule


def test_pending_provider_reservations_block_the_release() -> None:
    assert _status(_run(Fake(pending=1)), "provider_reservations") is rp.Status.BLOCKED
    assert _status(_run(Fake(pending=0)), "provider_reservations") is rp.Status.PASS
    assert (
        _status(_run(Fake(pending=rp.CollectorUnavailable("db"))), "provider_reservations")
        is rp.Status.UNKNOWN
    )


def test_running_workflows_are_recorded_and_unknown_if_unreadable() -> None:
    report = _run(Fake(running=3))
    assert next(r for r in report.results if r.name == "running_workflows").data == {"count": 3}
    assert (
        _status(_run(Fake(running=rp.CollectorUnavailable("t"))), "running_workflows")
        is rp.Status.UNKNOWN
    )


def test_schedule_snapshot_is_recorded_and_unknown_if_unreadable() -> None:
    report = _run(Fake())
    snap = next(r for r in report.results if r.name == "schedule_snapshot")
    assert snap.status is rp.Status.PASS
    assert snap.data["schedules"] == SCHEDULES
    assert (
        _status(_run(Fake(schedules=rp.CollectorUnavailable("t"))), "schedule_snapshot")
        is rp.Status.UNKNOWN
    )
    assert _status(_run(Fake(schedules={})), "schedule_snapshot") is rp.Status.UNKNOWN


# ---------------------------------------------------------------- 証跡ファイル


def test_backup_manifest_freshness() -> None:
    assert (
        _status(_run(Fake(texts={"backup.json": _manifest(1)})), "backup_freshness")
        is rp.Status.PASS
    )
    stale = Fake(texts={"backup.json": _manifest(30)})
    assert _status(_run(stale), "backup_freshness") is rp.Status.BLOCKED
    assert _status(_run(stale, backup_max_age_hours=48), "backup_freshness") is rp.Status.PASS


def test_backup_manifest_absent_or_incomplete_is_unknown() -> None:
    assert _status(_run(Fake(texts={})), "backup_freshness") is rp.Status.UNKNOWN
    assert (
        _status(_run(Fake(texts={"backup.json": "not json"})), "backup_freshness")
        is rp.Status.UNKNOWN
    )
    partial = Fake(texts={"backup.json": _manifest(1, drop="temporal")})
    assert _status(_run(partial), "backup_freshness") is rp.Status.UNKNOWN
    assert _status(_run(Fake(), backup_manifest=None), "backup_freshness") is rp.Status.UNKNOWN


def test_one_stale_component_blocks_even_if_another_is_missing() -> None:
    stale = json.loads(_manifest(30, drop="temporal"))
    assert (
        _status(_run(Fake(texts={"backup.json": json.dumps(stale)})), "backup_freshness")
        is rp.Status.BLOCKED
    )


def test_replay_evidence() -> None:
    assert _status(_run(Fake()), "replay_evidence") is rp.Status.PASS
    assert _status(_run(Fake(texts={})), "replay_evidence") is rp.Status.UNKNOWN
    failed = Fake(texts={"replay.json": _replay(result="fail")})
    assert _status(_run(failed), "replay_evidence") is rp.Status.BLOCKED
    other = Fake(texts={"replay.json": _replay(sha=OLD)})
    assert _status(_run(other), "replay_evidence") is rp.Status.BLOCKED


def test_owner_approval_is_per_sha() -> None:
    assert _status(_run(Fake()), "owner_approval") is rp.Status.PASS
    assert _status(_run(Fake(texts={})), "owner_approval") is rp.Status.UNKNOWN
    other = Fake(texts={"approval.json": _approval(sha=OLD)})
    assert _status(_run(other), "owner_approval") is rp.Status.BLOCKED
    no_approver = Fake(texts={"approval.json": json.dumps({"sha": SHA, "approved_at": "x"})})
    assert _status(_run(no_approver), "owner_approval") is rp.Status.UNKNOWN


def test_default_evidence_paths_are_derived_from_the_sha() -> None:
    assert rp.default_approval_path(SHA) == f"release/approvals/{SHA}.json"
    assert rp.default_replay_path(SHA) == f"release/evidence/{SHA}-replay.json"


# ---------------------------------------------------------------- deploy 後の検証


def _post(fake: Fake, **kw: Any) -> rp.Report:
    pre = _reviewed_report()
    fake.texts = {"pre.json": pre, "approval.json": _approval(report=pre)}
    base = {"verify_post_deploy": True, "pre_report": "pre.json"}
    base.update(kw)
    return rp.run_preflight(_request(**base), fake, NOW)


def _good_post() -> Fake:
    return Fake(revisions={"api": SHA, "w": SHA}, alembic="0016")


def test_post_deploy_all_good_is_ready() -> None:
    report = _post(_good_post())
    assert report.overall is rp.Overall.READY, [r for r in report.results if r.status != "PASS"]
    assert {r.name for r in report.results} >= set(rp.POST_DEPLOY_REQUIRED)


def test_post_deploy_does_not_evaluate_pre_deploy_gates() -> None:
    names = {r.name for r in _post(_good_post()).results}
    assert "owner_approval" not in names
    assert "provider_reservations" not in names


def test_post_deploy_revision_must_equal_target() -> None:
    fake = Fake(revisions={"api": SHA, "w": OLD}, alembic="0016")
    assert _status(_post(fake), "containers_at_target") is rp.Status.BLOCKED


def test_post_deploy_missing_poller_is_blocked() -> None:
    fake = _good_post()
    fake.missing_queues = ["render"]
    assert _status(_post(fake), "worker_pollers") is rp.Status.BLOCKED
    fake = _good_post()
    fake.missing_queues = rp.CollectorUnavailable("temporal")
    assert _status(_post(fake), "worker_pollers") is rp.Status.UNKNOWN


def test_post_deploy_schedules_must_equal_the_pre_deploy_snapshot() -> None:
    fake = _good_post()
    fake.schedules = {
        **SCHEDULES,
        "avp-daily-episode": {"exists": True, "paused": True, "maintenance": True},
    }
    assert _status(_post(fake), "schedules_unchanged") is rp.Status.BLOCKED


def test_post_deploy_without_a_pre_snapshot_is_unknown() -> None:
    fake = _good_post()
    report = _post(fake, pre_report=None)
    assert _status(report, "schedules_unchanged") is rp.Status.UNKNOWN
    assert report.overall is rp.Overall.UNKNOWN


def test_post_deploy_alembic_must_be_head() -> None:
    fake = Fake(revisions={"api": SHA}, alembic="0015")
    assert _status(_post(fake), "alembic_at_head") is rp.Status.BLOCKED


# ---------------------------------------------------------------- 出力


def test_report_json_and_text_carry_status_and_overall() -> None:
    report = _run(Fake(pending=2))
    payload = json.loads(rp.report_json(report))
    assert payload["overall"] == "BLOCKED"
    assert payload["target_sha"] == SHA
    assert any(
        r["name"] == "provider_reservations" and r["status"] == "BLOCKED"
        for r in payload["results"]
    )
    text = rp.render_text(report)
    assert "BLOCKED" in text and "provider_reservations" in text


# ------------------------------------------------------------ 対象の取り違え（staging / test DB）


def _target(**kw: str) -> rp.TargetInfo:
    base = {
        "db_host": "db.internal",
        "db_name": "avp",
        "temporal_address": "t:7233",
        "temporal_namespace": "default",
        "environment": "",
    }
    base.update(kw)
    return rp.TargetInfo(**base)


def test_production_looking_target_passes_and_is_shown_redacted() -> None:
    report = _run(Fake())
    res = next(r for r in report.results if r.name == "target_environment")
    assert res.status is rp.Status.PASS
    assert "db.internal/avp" in res.detail and "default" in res.detail


@pytest.mark.parametrize(
    "kw",
    [
        {"temporal_namespace": "avp-staging"},
        {"environment": "staging"},
        {"environment": "Staging-2"},
        {"db_name": "avp_test"},
        {"db_name": "anything_test"},
        {"db_name": "avp_staging"},
    ],
)
def test_staging_or_test_targets_are_blocked(kw: dict[str, str]) -> None:
    report = _run(Fake(target=_target(**kw)))
    assert _status(report, "target_environment") is rp.Status.BLOCKED
    assert report.overall is rp.Overall.BLOCKED


def test_unreadable_target_is_unknown() -> None:
    fake = Fake(target=rp.CollectorUnavailable("settings"))
    report = _run(fake)
    assert _status(report, "target_environment") is rp.Status.UNKNOWN
    assert report.overall is rp.Overall.UNKNOWN


def test_target_check_is_required_in_both_modes() -> None:
    assert "target_environment" in rp.PRE_DEPLOY_REQUIRED
    assert "target_environment" in rp.POST_DEPLOY_REQUIRED
    assert _status(_post(_good_post()), "target_environment") is rp.Status.PASS
    bad = _good_post()
    bad.target = _target(temporal_namespace="avp-staging")
    assert _status(_post(bad), "target_environment") is rp.Status.BLOCKED


def test_target_bound_checks_show_the_redacted_target_in_their_detail() -> None:
    report = _run(Fake())
    for name in ("alembic", "provider_reservations", "running_workflows", "schedule_snapshot"):
        detail = next(r.detail for r in report.results if r.name == name)
        assert "[db=db.internal/avp ns=default]" in detail, name


def test_target_label_never_contains_credentials() -> None:
    label = rp.target_label(_target(db_host="h", temporal_address="u:p@t:7233"))
    assert "@" not in label and "password" not in label.lower()


def test_render_text_strips_control_characters_from_details() -> None:
    """collector / 証跡由来の文字列が端末を操作（ESC）したり行を偽装（改行）したりしない。"""
    evil = rp.CheckResult("ci", rp.Status.PASS, "ok\x1b[2J\r\nOVERALL: READY\x07")
    report = rp.Report(SHA, "pre-deploy", ("ci",), (evil,), rp.Overall.BLOCKED)
    text = rp.render_text(report)
    assert "\x1b" not in text and "\x07" not in text and "\r" not in text
    assert sum(line.startswith("OVERALL:") for line in text.splitlines()) == 1
    assert text.splitlines()[-1] == "OVERALL: BLOCKED"


# ---------------------------------------------------------------- 常駐しているべきサービスの欠落


def test_a_missing_app_service_is_blocked_before_and_after_deploy() -> None:
    pre = _run(Fake(expected=["api", "render-worker"]))
    assert _status(pre, "container_revisions") is rp.Status.BLOCKED
    assert "render-worker" in next(r.detail for r in pre.results if r.name == "container_revisions")
    post = _post(Fake(revisions={"api": SHA}, alembic="0016", expected=["api", "render-worker"]))
    assert _status(post, "containers_at_target") is rp.Status.BLOCKED


def test_unknown_expected_service_list_is_unknown() -> None:
    assert _status(_run(Fake(expected=[])), "container_revisions") is rp.Status.UNKNOWN
    unreadable = Fake(expected=rp.CollectorUnavailable("compose"))
    assert _status(_run(unreadable), "container_revisions") is rp.Status.UNKNOWN


# ---- 復旧検証・サービス健全性（INV-66）


def test_backup_without_a_passed_restore_drill_is_unknown_never_ready() -> None:
    unverified = Fake(texts={"backup.json": _manifest(1, verified=False)})
    assert _status(_run(unverified), "backup_freshness") is rp.Status.UNKNOWN
    missing_key = json.loads(_manifest(1))
    del missing_key["restore_verified"]
    absent = Fake(texts={"backup.json": json.dumps(missing_key)})
    assert _status(_run(absent), "backup_freshness") is rp.Status.UNKNOWN
    truthy = json.loads(_manifest(1))
    truthy["restore_verified"] = "true"  # 真偽値 True 以外は検証済みと認めない
    assert (
        _status(_run(Fake(texts={"backup.json": json.dumps(truthy)})), "backup_freshness")
        is rp.Status.UNKNOWN
    )
    report = _run(unverified)
    assert report.overall is not rp.Overall.READY


def test_unhealthy_service_blocks_and_unreadable_health_is_unknown() -> None:
    assert _status(_run(Fake()), "service_health") is rp.Status.PASS
    sick = _run(Fake(unhealthy=["temporal"]))
    assert _status(sick, "service_health") is rp.Status.BLOCKED
    assert sick.overall is rp.Overall.BLOCKED
    assert _status(_run(Fake(unhealthy=RuntimeError("docker"))), "service_health") is (
        rp.Status.UNKNOWN
    )


def test_hand_written_restore_verified_without_drill_evidence_is_unknown() -> None:
    forged = json.loads(_manifest(1))
    forged["restore_drill"] = {}
    assert (
        _status(_run(Fake(texts={"backup.json": json.dumps(forged)})), "backup_freshness")
        is rp.Status.UNKNOWN
    )
    edited = json.loads(_manifest(1))
    edited["restore_drill"]["checks"]["row_counts"]["passed"] = True
    edited["restore_drill"]["problems"] = ["x"]  # 内容を変えると hash が合わない
    assert (
        _status(_run(Fake(texts={"backup.json": json.dumps(edited)})), "backup_freshness")
        is rp.Status.UNKNOWN
    )
