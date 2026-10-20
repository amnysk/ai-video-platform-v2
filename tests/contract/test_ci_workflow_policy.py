"""CI ワークフローの方針を機械で固定する（ADR-0049）。

この検査が守るもの:

- ``permissions`` は最小権限（workflow 全体で ``contents: read`` のみ）。
- push / PR のトリガが release 系ブランチを覆う。``push: [main]`` だけだった頃は、
  実在しない ``main`` を待つ間、統合ブランチへの push で CI が一度も走らなかった。
- 集約 job ``CI gate`` が **他の全 job** を ``needs`` に持つ。branch protection の
  required check を ``CI gate`` ひとつにするので、job を足して gate に入れ忘れると
  その job は誰も見ない検査になる。この検査がそれを機械で止める。
- 何もしない job（``true`` だけ）で必須チェックを緑にしない。
"""

from __future__ import annotations

import pathlib
from typing import Any

import yaml

from contracts.ci import CI_GATE_JOB_NAME

REPO = pathlib.Path(__file__).resolve().parents[2]
CI = REPO / ".github" / "workflows" / "ci.yml"
GATE_ID = "gate"
GATE_NAME = CI_GATE_JOB_NAME  # 唯一の宣言元は contracts/ci.py（release preflight も同じ値を読む）

#: required check になる job 名。ここを書き換える＝branch protection の設定変更なので、
#: 名前を変えるときは docs/operations/branch-strategy.md の payload も同時に直すこと。
STABLE_JOB_NAMES = {
    "Ruff",
    "Type check",
    "Unit tests",
    "Contract tests",
    "Architecture tests",
    "Integration tests",
    "Docker build",
    "Alembic consistency",
    "Temporal replay",
    "Safety contracts",
    GATE_NAME,
}


def _workflow() -> dict[str, Any]:
    data = yaml.safe_load(CI.read_text(encoding="utf-8"))
    assert isinstance(data, dict)
    # YAML 1.1 では素の `on` が真偽値 True に解釈される
    if True in data:
        data["on"] = data.pop(True)
    return data


def _jobs() -> dict[str, dict[str, Any]]:
    return _workflow()["jobs"]


def test_permissions_are_least_privilege() -> None:
    wf = _workflow()
    assert wf.get("permissions") == {"contents": "read"}, "top-level permissions: contents: read"
    for job_id, job in _jobs().items():
        perms = job.get("permissions", {})
        assert isinstance(perms, dict), f"{job_id}: permissions は map で書く"
        writes = {k: v for k, v in perms.items() if v != "read" and v != "none"}
        assert not writes, f"{job_id}: write 権限を足さない: {writes}"


def test_triggers_cover_integration_and_release_branches() -> None:
    on = _workflow()["on"]
    assert "workflow_dispatch" in on
    pr = on["pull_request"]
    assert not pr or "branches" not in pr, "pull_request は全 base branch を対象にする"
    pushed = set(on["push"]["branches"])
    assert {"main", "develop", "release/**", "hotfix/**"} <= pushed


def test_job_names_required_by_branch_protection_exist_and_are_stable() -> None:
    names = {job["name"] for job in _jobs().values()}
    assert names >= STABLE_JOB_NAMES, f"missing: {sorted(STABLE_JOB_NAMES - names)}"


def test_ci_gate_needs_every_other_job() -> None:
    jobs = _jobs()
    gate = jobs[GATE_ID]
    assert gate["name"] == GATE_NAME
    others = set(jobs) - {GATE_ID}
    needs = gate["needs"]
    assert isinstance(needs, list)
    assert set(needs) == others, (
        f"CI gate の needs と job 一覧が食い違う。足りない: {sorted(others - set(needs))} "
        f"余分: {sorted(set(needs) - others)}"
    )


def test_ci_gate_runs_always_and_fails_on_anything_but_success() -> None:
    gate = _jobs()[GATE_ID]
    assert "always()" in str(gate.get("if", "")), "needs が落ちても gate 自身は評価される"
    script = "\n".join(str(s.get("run", "")) for s in gate["steps"])
    assert "toJSON(needs)" in str(gate), "needs 全部の result を読む"
    # success 以外（failure / cancelled / skipped）を全て落とす書き方であること
    assert '!= "success"' in script
    assert "exit(1)" in script or "exit 1" in script


def test_no_job_is_a_no_op() -> None:
    for job_id, job in _jobs().items():
        if job_id == GATE_ID:
            continue
        runs = [str(s["run"]).strip() for s in job["steps"] if "run" in s]
        meaningful = [r for r in runs if r not in {"true", ":", "exit 0"}]
        assert meaningful, f"{job_id}: 実質何もしない job"
        swallowed = any(r.rstrip().endswith("|| true") for r in runs)
        assert not swallowed, f"{job_id}: `|| true` で失敗を握りつぶさない"


def _steps_with_job() -> list[tuple[str, dict[str, Any]]]:
    return [(jid, s) for jid, job in _jobs().items() for s in job.get("steps", [])]


def _runs_under_pipefail(job: dict[str, Any], step: dict[str, Any]) -> bool:
    """`shell: bash` は `bash -eo pipefail`。未指定の既定は `bash -e`（pipefail 無し）。"""
    shell = (
        step.get("shell")
        or job.get("defaults", {}).get("run", {}).get("shell")
        or _workflow().get("defaults", {}).get("run", {}).get("shell")
    )
    if shell == "bash" or (isinstance(shell, str) and "pipefail" in shell):
        return True
    return "set -o pipefail" in str(step.get("run", "")) or "set -eo pipefail" in str(
        step.get("run", "")
    )


def test_every_tee_step_runs_under_pipefail() -> None:
    """`pytest | tee log` は pipefail が無いと pytest の失敗が tee の 0 に隠れて job が緑になる。"""
    tee_steps = 0
    for jid, step in _steps_with_job():
        if "| tee" in str(step.get("run", "")):
            tee_steps += 1
            assert _runs_under_pipefail(_jobs()[jid], step), f"{jid}: `| tee` が pipefail 無し"
    assert tee_steps >= 2, "検査対象の tee step が見つからない（検査自体が空振りしていないか）"


def test_every_teed_pytest_log_is_checked_for_passed_tests() -> None:
    """pytest が 0 件で終わる（収集失敗・全 skip）ケースを、ログの `N passed` で止める。"""
    for jid, job in _jobs().items():
        steps = job.get("steps", [])
        for step in steps:
            run = str(step.get("run", ""))
            if "pytest" not in run or "| tee" not in run:
                continue
            log = run.split("| tee", 1)[1].split()[0]
            later = "\n".join(str(s.get("run", "")) for s in steps[steps.index(step) + 1 :])
            assert log in later and "[0-9]+ passed" in later, f"{jid}: {log} に passed の検査が無い"


def test_secrets_context_is_not_used_in_if_conditions() -> None:
    # `if:` では secrets コンテキストが使えない。使うと workflow file issue になり
    # 全 job が 0 秒で失敗する（job の env 経由で参照する）
    import re

    text = CI.read_text(encoding="utf-8")
    bad = [
        line.strip()
        for line in text.splitlines()
        if re.match(r"\s*(-\s+)?if:", line) and "secrets." in line
    ]
    assert not bad, bad
