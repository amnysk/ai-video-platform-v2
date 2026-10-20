"""承認・証跡の鮮度と束縛、pre-report の検証、rollback モード（INV-65 / ADR-0050）。

承認ファイルは自己申告で、認証ではない。ここで守るのは「古い承認・別の preflight 出力・
別 SHA の承認の流用」という取り違えだけ。
"""

from __future__ import annotations

import json
from datetime import timedelta
from typing import Any

import pytest

from domain import release_preflight as rp
from tests.unit.test_release_preflight import (
    BASE_MIGRATION,
    NOW,
    OLD,
    ROOT_MIGRATION,
    SAFE_MIGRATION,
    SHA,
    Fake,
    _approval,
    _good_post,
    _post,
    _replay,
    _request,
    _reviewed_report,
    _run,
    _status,
)

# ---------------------------------------------------------------- 承認の鮮度


def _with_approval(text: str) -> Fake:
    fake = Fake()
    fake.texts = {**fake.texts, "approval.json": text}
    return fake


def test_fresh_approval_passes() -> None:
    assert _status(_run(Fake()), "owner_approval") is rp.Status.PASS


def test_approval_from_the_future_is_blocked() -> None:
    soon = _with_approval(_approval(at=NOW + timedelta(hours=1)))
    assert _status(_run(soon), "owner_approval") is rp.Status.BLOCKED
    skew = _with_approval(_approval(at=NOW + timedelta(minutes=2)))
    assert _status(_run(skew), "owner_approval") is rp.Status.PASS


def test_approval_older_than_the_max_age_is_blocked_and_the_age_is_configurable() -> None:
    old = _with_approval(_approval(at=NOW - timedelta(hours=73)))
    assert _status(_run(old), "owner_approval") is rp.Status.BLOCKED
    assert _status(_run(old, evidence_max_age_hours=100), "owner_approval") is rp.Status.PASS
    ok = _with_approval(_approval(at=NOW - timedelta(hours=71)))
    assert _status(_run(ok), "owner_approval") is rp.Status.PASS


def test_approval_without_a_timezone_is_unknown() -> None:
    naive = _with_approval(_approval().replace("+00:00", ""))
    assert _status(_run(naive), "owner_approval") is rp.Status.UNKNOWN


@pytest.mark.parametrize("hours", [0, -1, 169, 1e9, float("nan")])
def test_evidence_max_age_is_bounded(hours: float) -> None:
    assert _status(_run(Fake(), evidence_max_age_hours=hours), "owner_approval") is (
        rp.Status.UNKNOWN
    )
    assert _status(_run(Fake(), evidence_max_age_hours=hours), "replay_evidence") is (
        rp.Status.UNKNOWN
    )


@pytest.mark.parametrize("hours", [0, -1, 721, float("nan")])
def test_backup_max_age_is_bounded(hours: float) -> None:
    assert _status(_run(Fake(), backup_max_age_hours=hours), "backup_freshness") is (
        rp.Status.UNKNOWN
    )


# ---------------------------------------------------------------- 承認と preflight 出力の束縛


def test_approval_must_carry_the_report_hash() -> None:
    doc = json.loads(_approval())
    del doc["report_sha256"]
    assert _status(_run(_with_approval(json.dumps(doc))), "owner_approval") is rp.Status.UNKNOWN


def test_approved_report_must_be_given_and_readable() -> None:
    assert _status(_run(Fake(), approved_report=None), "owner_approval") is rp.Status.UNKNOWN
    fake = Fake()
    del fake.texts["report.json"]
    assert _status(_run(fake), "owner_approval") is rp.Status.UNKNOWN


def test_report_changed_after_approval_is_blocked() -> None:
    fake = Fake()
    fake.texts = {**fake.texts, "report.json": _reviewed_report() + " "}
    assert _status(_run(fake), "owner_approval") is rp.Status.BLOCKED


def test_approved_report_must_be_for_this_sha_and_mode() -> None:
    for report in (
        _reviewed_report(sha=OLD),
        _reviewed_report(mode="post-deploy"),
        _reviewed_report(broken="ci"),
        _reviewed_report(overall="BLOCKED"),
    ):
        fake = Fake()
        fake.texts = {
            **fake.texts,
            "report.json": report,
            "approval.json": _approval(report=report),
        }
        assert _status(_run(fake), "owner_approval") is rp.Status.BLOCKED, report[:80]


def test_unparseable_approved_report_is_unknown() -> None:
    fake = Fake()
    fake.texts = {
        **fake.texts,
        "report.json": "not json",
        "approval.json": _approval(report="not json"),
    }
    assert _status(_run(fake), "owner_approval") is rp.Status.UNKNOWN


# ---------------------------------------------------------------- replay 証跡の鮮度


def _replay_at(delta: timedelta) -> str:
    return json.dumps({"sha": SHA, "result": "pass", "at": (NOW + delta).isoformat()})


def test_replay_evidence_must_be_dated_and_fresh() -> None:
    def status(text: str) -> rp.Status:
        fake = Fake()
        fake.texts = {**fake.texts, "replay.json": text}
        return _status(_run(fake), "replay_evidence")

    assert status(_replay()) is rp.Status.PASS
    assert status(_replay_at(timedelta(hours=-80))) is rp.Status.BLOCKED
    assert status(_replay_at(timedelta(hours=2))) is rp.Status.BLOCKED
    assert status(json.dumps({"sha": SHA, "result": "pass"})) is rp.Status.UNKNOWN


# ------------------------------------------------------------ deploy 後: 承認の束縛と pre-report


def test_post_deploy_approval_must_be_bound_to_the_pre_report() -> None:
    assert _status(_post(_good_post()), "approval_binding") is rp.Status.PASS
    fake = _good_post()
    other = _reviewed_report() + " "
    report = _post(fake)
    assert report.overall is rp.Overall.READY
    fake.texts = {**fake.texts, "pre.json": other}
    assert (
        _status(rp.run_preflight(_post_request(), fake, NOW), "approval_binding")
        is rp.Status.BLOCKED
    )


def _post_request(**kw: Any) -> rp.Request:
    return _request(verify_post_deploy=True, pre_report="pre.json", **kw)


def test_post_deploy_approval_missing_or_for_another_sha() -> None:
    fake = _good_post()
    _post(fake)
    del fake.texts["approval.json"]
    got = rp.run_preflight(_post_request(), fake, NOW)
    assert _status(got, "approval_binding") is rp.Status.UNKNOWN
    fake.texts["approval.json"] = _approval(sha=OLD)
    got = rp.run_preflight(_post_request(), fake, NOW)
    assert _status(got, "approval_binding") is rp.Status.BLOCKED


@pytest.mark.parametrize(
    "pre",
    [
        _reviewed_report(sha=OLD),
        _reviewed_report(mode="rollback-pre-deploy"),
        _reviewed_report(overall="BLOCKED"),
    ],
)
def test_post_deploy_pre_report_must_be_for_this_deploy(pre: str) -> None:
    fake = _good_post()
    _post(fake)
    fake.texts = {"pre.json": pre, "approval.json": _approval(report=pre)}
    got = rp.run_preflight(_post_request(), fake, NOW)
    assert _status(got, "schedules_unchanged") is rp.Status.BLOCKED
    assert got.overall is rp.Overall.BLOCKED


def test_post_deploy_unparseable_pre_report_is_unknown() -> None:
    fake = _good_post()
    _post(fake)
    fake.texts = {"pre.json": "nope", "approval.json": _approval(report="nope")}
    got = rp.run_preflight(_post_request(), fake, NOW)
    assert _status(got, "schedules_unchanged") is rp.Status.UNKNOWN


# ---------------------------------------------------------------- rollback

#: 本番は 0016 まで migrate 済み（今動いている旧コードは知っている）。rollback 先 SHA は 0015 まで
AHEAD = {
    OLD: {"0014.py": ROOT_MIGRATION, "0015.py": BASE_MIGRATION, "0016.py": SAFE_MIGRATION},
    SHA: {"0014.py": ROOT_MIGRATION, "0015.py": BASE_MIGRATION},
}
ROLLBACK_OK = {"rollback": {"old_code_compatible": True, "revisions": ["0016"]}}


def _rollback_fake(extra: dict[str, Any] | None = None, **kw: Any) -> Fake:
    report = _reviewed_report(mode="rollback-pre-deploy")
    fake = Fake(
        files=kw.pop("files", AHEAD),
        alembic=kw.pop("alembic", "0016"),
        relation=kw.pop("relation", rp.Relation.BEHIND),
        **kw,
    )
    fake.texts = {
        **fake.texts,
        "report.json": report,
        "approval.json": _approval(report=report, extra=extra),
    }
    return fake


def _rb(fake: Fake, **kw: Any) -> rp.Report:
    return rp.run_preflight(_request(rollback=True, **kw), fake, NOW)


def test_db_ahead_of_target_blocks_a_normal_preflight() -> None:
    report = _run(_rollback_fake(ROLLBACK_OK))
    assert _status(report, "alembic") is rp.Status.BLOCKED
    assert _status(report, "ancestry") is rp.Status.BLOCKED


def test_rollback_with_expand_only_ahead_migrations_and_owner_flag_is_ready() -> None:
    report = _rb(_rollback_fake(ROLLBACK_OK))
    assert report.mode == "rollback-pre-deploy"
    assert _status(report, "alembic") is rp.Status.PASS
    assert _status(report, "ancestry") is rp.Status.PASS
    assert _status(report, "migration_compat") is rp.Status.PASS
    assert report.overall is rp.Overall.READY, [r for r in report.results if r.status != "PASS"]
    data = next(r.data for r in report.results if r.name == "alembic")
    assert data["rollback_ahead"] == ["0016"] and data["production"] == "0016"


def test_rollback_without_the_owner_flag_is_unknown() -> None:
    report = _rb(_rollback_fake())
    assert _status(report, "alembic") is rp.Status.UNKNOWN
    assert report.overall is rp.Overall.UNKNOWN


def test_rollback_flag_false_or_for_other_revisions_is_blocked() -> None:
    no = {"rollback": {"old_code_compatible": False, "revisions": ["0016"]}}
    assert _status(_rb(_rollback_fake(no)), "alembic") is rp.Status.BLOCKED
    other = {"rollback": {"old_code_compatible": True, "revisions": ["0099"]}}
    assert _status(_rb(_rollback_fake(other)), "alembic") is rp.Status.BLOCKED
    truthy = {"rollback": {"old_code_compatible": "yes", "revisions": ["0016"]}}
    assert _status(_rb(_rollback_fake(truthy)), "alembic") is rp.Status.BLOCKED


def test_rollback_is_blocked_when_an_ahead_migration_was_a_contract_migration() -> None:
    destructive = SAFE_MIGRATION.replace(
        'op.add_column("t", sa.Column("x", sa.Text(), nullable=True))', 'op.drop_column("t", "x")'
    )
    files = {**AHEAD, OLD: {**AHEAD[OLD], "0016.py": destructive}}
    report = _rb(_rollback_fake(ROLLBACK_OK, files=files))
    assert _status(report, "alembic") is rp.Status.BLOCKED


def test_rollback_is_blocked_when_production_is_unknown_to_the_running_code_too() -> None:
    assert _status(_rb(_rollback_fake(ROLLBACK_OK, alembic="9999")), "alembic") is rp.Status.BLOCKED


def test_rollback_is_blocked_when_the_target_head_is_not_an_ancestor_of_production() -> None:
    other_head = (
        'revision = "0015x"\ndown_revision = "0014"\n\n\ndef upgrade() -> None:\n    pass\n'
    )
    files = {**AHEAD, SHA: {"0014.py": ROOT_MIGRATION, "0015x.py": other_head}}
    assert _status(_rb(_rollback_fake(ROLLBACK_OK, files=files)), "alembic") is rp.Status.BLOCKED


def test_rollback_without_an_approval_file_is_unknown() -> None:
    fake = _rollback_fake(ROLLBACK_OK)
    del fake.texts["approval.json"]
    assert _status(_rb(fake), "alembic") is rp.Status.UNKNOWN


def test_rollback_diverged_still_needs_allow_non_ff() -> None:
    fake = _rollback_fake(ROLLBACK_OK, relation=rp.Relation.DIVERGED)
    assert _status(_rb(fake), "ancestry") is rp.Status.BLOCKED
    assert _status(_rb(fake, allow_non_ff=True), "ancestry") is rp.Status.PASS


def test_rollback_still_blocks_edited_in_place_migrations() -> None:
    files = {**AHEAD, SHA: {"0014.py": ROOT_MIGRATION, "0015.py": BASE_MIGRATION + "# x\n"}}
    report = _rb(_rollback_fake(ROLLBACK_OK, files=files))
    assert _status(report, "migration_compat") is rp.Status.BLOCKED


def _rollback_post(production: str, *, pre: str | None = None) -> rp.Report:
    report_text = json.loads(_reviewed_report(mode="rollback-pre-deploy"))
    for r in report_text["results"]:
        if r["name"] == "alembic":
            r["data"] = {"production": "0016", "head": "0015", "rollback_ahead": ["0016"]}
    text = json.dumps(report_text)
    fake = Fake(revisions={"api": SHA, "w": SHA}, alembic=production)
    fake.texts = {"pre.json": text if pre is None else pre, "approval.json": _approval(report=text)}
    return rp.run_preflight(_post_request(rollback=True), fake, NOW)


def test_rollback_post_deploy_requires_the_db_revision_unchanged() -> None:
    good = _rollback_post("0016")
    assert good.mode == "rollback-post-deploy"
    assert _status(good, "alembic_at_head") is rp.Status.PASS
    assert good.overall is rp.Overall.READY, [r for r in good.results if r.status != "PASS"]
    assert _status(_rollback_post("0015"), "alembic_at_head") is rp.Status.BLOCKED


def test_rollback_post_deploy_without_the_alembic_record_is_unknown() -> None:
    plain = _reviewed_report(mode="rollback-pre-deploy")
    assert _status(_rollback_post("0016", pre=plain), "alembic_at_head") is rp.Status.UNKNOWN
