# ruff: noqa: E501
"""scripts/log-recoverability.py: 停止期間の json-file ログの取り戻せる範囲（読み取り専用）。

理由は docs/testing/log-recoverability-rationale.md。偽の docker（ps / inspect）と tmp の containers dir だけを使う。
"""

from __future__ import annotations

import hashlib
import importlib.util
import json
import sys
from pathlib import Path
from types import ModuleType

import pytest

ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / "scripts" / "log-recoverability.py"

FAKE = """#!{py} -SE
import json, os, sys
st = json.load(open(os.environ["FAKE_DOCKER_STATE"]))
if sys.argv[1] == "ps":
    for c in st:
        print(c["id"] + "\\t" + c["name"])
elif sys.argv[1] == "inspect":
    print(next(c for c in st if c["id"] == sys.argv[-1])["created"])
else:
    sys.exit(2)
"""


@pytest.fixture(scope="module")
def tool() -> ModuleType:
    spec = importlib.util.spec_from_file_location("avp_log_recoverability", SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


class Env:
    def __init__(self, tmp: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        self.cdir = tmp / "containers"
        self.cdir.mkdir()
        self.out = tmp / "backfill"
        self.docker = tmp / "docker"
        self.docker.write_text(FAKE.format(py=sys.executable))
        self.docker.chmod(0o755)
        self.state = tmp / "fake.json"
        self.containers: list[dict[str, str]] = []
        monkeypatch.setenv("FAKE_DOCKER_STATE", str(self.state))

    def add(self, cid: str, name: str, created: str, files: dict[str, list[str]]) -> None:
        self.containers.append({"id": cid, "name": name, "created": created})
        self.state.write_text(json.dumps(self.containers))
        d = self.cdir / cid
        d.mkdir()
        for fname, times in files.items():
            (d / f"{cid}-{fname}").write_text(
                "".join(
                    json.dumps({"log": "x\n", "stream": "stdout", "time": t}) + "\n" for t in times
                )
            )

    def run(self, tool: ModuleType, *extra: str) -> int:
        return tool.main(
            ["--since", "2026-10-20T15:00:00Z", "--until", "2026-10-24T15:00:00Z",
             "--docker", str(self.docker), "--containers-dir", str(self.cdir), *extra]
        )  # fmt: skip


@pytest.fixture
def env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Env:
    return Env(tmp_path, monkeypatch)


def _report(env: Env) -> dict[str, dict[str, object]]:
    m = json.loads((env.out / "manifest.json").read_text())
    return {c["container"]: c for c in m["containers"]}


def test_rotated_away_start_of_the_window_is_unrecoverable(tool: ModuleType, env: Env) -> None:
    env.add("a" * 64, "temporal", "2026-09-01T00:00:00Z",
            {"json.log": ["2026-10-22T00:00:00.123456789Z", "2026-10-23T00:00:00Z"]})  # fmt: skip
    assert env.run(tool, "--out", str(env.out)) == 1
    r = _report(env)["temporal"]
    assert r["recoverable"] == {"from": "2026-10-22T00:00:00Z", "to": "2026-10-23T00:00:00Z"}
    assert r["unrecoverable"] == [{"from": "2026-10-20T15:00:00Z", "to": "2026-10-22T00:00:00Z"}]


def test_fully_retained_window_exits_zero(tool: ModuleType, env: Env) -> None:
    env.add("b" * 64, "worker", "2026-09-01T00:00:00Z",
            {"json.log": ["2026-10-20T14:59:00Z", "2026-10-24T15:30:00Z"]})  # fmt: skip
    assert env.run(tool, "--out", str(env.out)) == 0
    assert _report(env)["worker"]["unrecoverable"] == []


def test_rotated_older_files_are_read(tool: ModuleType, env: Env) -> None:
    env.add("c" * 64, "pg", "2026-09-01T00:00:00Z",
            {"json.log.1": ["2026-10-20T14:59:30Z", "2026-10-20T15:00:30Z"], "json.log": ["2026-10-24T00:00:00Z"]})  # fmt: skip
    assert env.run(tool, "--out", str(env.out)) == 0
    assert _report(env)["pg"]["lines_in_window"] == 2


def test_container_without_any_log_file_is_wholly_unrecoverable(tool: ModuleType, env: Env) -> None:
    env.add("d" * 64, "gone", "2026-09-01T00:00:00Z", {})
    assert env.run(tool, "--out", str(env.out)) == 1
    r = _report(env)["gone"]
    assert r["recoverable"] is None and len(r["unrecoverable"]) == 1  # type: ignore[arg-type]


def test_window_starts_at_container_creation(tool: ModuleType, env: Env) -> None:
    """作る前のログは存在しないので「失われた」に数えない。"""
    env.add("e" * 64, "new", "2026-10-22T00:00:00Z", {"json.log": ["2026-10-22T00:00:05Z"]})
    env.run(tool, "--out", str(env.out))
    r = _report(env)["new"]
    assert r["window"]["from"] == "2026-10-22T00:00:00Z"  # type: ignore[index]
    assert r["unrecoverable"] == [{"from": "2026-10-22T00:00:00Z", "to": "2026-10-22T00:00:05Z"}]


def test_backfill_copies_only_window_lines_verbatim_and_never_writes_the_source(
    tool: ModuleType, env: Env
) -> None:
    env.add("f" * 64, "app", "2026-09-01T00:00:00Z",
            {"json.log": ["2026-10-19T00:00:00Z", "2026-10-21T00:00:00Z", "2026-10-25T00:00:00Z"]})  # fmt: skip
    src = env.cdir / ("f" * 64) / ("f" * 64 + "-json.log")
    before = hashlib.sha256(src.read_bytes()).hexdigest()
    env.run(tool, "--out", str(env.out))
    lines = (env.out / "app.jsonl").read_text().splitlines()
    assert len(lines) == 1 and json.loads(lines[0])["time"] == "2026-10-21T00:00:00Z"
    assert (env.out / "app.jsonl").stat().st_mode & 0o777 == 0o600
    assert hashlib.sha256(src.read_bytes()).hexdigest() == before


def test_report_only_mode_writes_no_files(tool: ModuleType, env: Env) -> None:
    env.add("1" * 64, "x", "2026-09-01T00:00:00Z", {"json.log": ["2026-10-21T00:00:00Z"]})
    env.run(tool)
    assert not env.out.exists()


def test_malformed_lines_are_counted_not_fatal(tool: ModuleType, env: Env) -> None:
    env.add("2" * 64, "m", "2026-09-01T00:00:00Z", {"json.log": ["2026-10-21T00:00:00Z"]})
    f = env.cdir / ("2" * 64) / ("2" * 64 + "-json.log")
    f.write_text(f.read_text() + "not json\n")
    env.run(tool, "--out", str(env.out))
    assert _report(env)["m"]["bad_lines"] == 1


def test_out_inside_the_containers_dir_is_refused(tool: ModuleType, env: Env) -> None:
    env.add("3" * 64, "y", "2026-09-01T00:00:00Z", {"json.log": ["2026-10-21T00:00:00Z"]})
    assert env.run(tool, "--out", str(env.cdir / "backfill")) == 2


def test_source_only_uses_read_only_docker_verbs() -> None:
    src = SCRIPT.read_text().split('"""', 2)[2]
    for banned in ('"stop"', '"start"', '"rm"', '"restart"', '"compose"', "urllib", "socket"):
        assert banned not in src, banned
