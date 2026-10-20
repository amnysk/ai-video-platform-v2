"""release/ の証跡は git に載せない（ADR-0050 / INV-65）。

deploy-workers.sh は ``git status --porcelain`` が空でなければ拒否する。承認・replay 証跡・
preflight 出力を ``release/`` に置くと、ignore されていない限り未追跡ファイルとして deploy を止める。
"""

from __future__ import annotations

import pathlib
import subprocess

REPO = pathlib.Path(__file__).resolve().parents[2]


def _git(cwd: pathlib.Path, *args: str) -> str:
    return subprocess.run(
        ["git", *args], cwd=cwd, capture_output=True, text=True, check=True
    ).stdout


def test_release_evidence_does_not_make_the_tree_dirty(tmp_path: pathlib.Path) -> None:
    """repo の .gitignore だけを持つ使い捨て repo で、deploy-workers.sh と同じ dirty 判定を再現する。"""
    _git(tmp_path, "init", "-q")
    (tmp_path / ".gitignore").write_text((REPO / ".gitignore").read_text(encoding="utf-8"))
    _git(tmp_path, "add", ".gitignore")
    _git(
        tmp_path, "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-qm", "base"
    )
    for rel in ("approvals/x.json", "evidence/x-replay.json", "records/t-preflight.json"):
        f = tmp_path / "release" / rel
        f.parent.mkdir(parents=True, exist_ok=True)
        f.write_text("{}")
    assert _git(tmp_path, "status", "--porcelain") == ""


def test_nothing_under_release_is_tracked() -> None:
    assert _git(REPO, "ls-files", "release").strip() == ""


def test_runbook_does_not_tell_to_commit_release_files() -> None:
    text = (REPO / "docs/operations/release-runbook.md").read_text(encoding="utf-8")
    assert "release/records/<tag>.md" not in text
    assert "docs/releases/<tag>.md" in text
