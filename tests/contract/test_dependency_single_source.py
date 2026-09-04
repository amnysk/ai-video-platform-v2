"""Python依存の source of truth は pyproject.toml ひとつ（AGENTS.md §8）。

この検査が無かったために、psycopg2 を足すとき pyproject.toml と Dockerfile の
**両方**を手で直す必要があり、片方だけ直せば無言で壊れる状態だった。
「同じ真実が2箇所に散る」＝このプロジェクトが繰り返し踏んできた事故の原型。
"""

from __future__ import annotations

import pathlib
import re
import tomllib

REPO = pathlib.Path(__file__).resolve().parents[2]
DOCKERFILE = REPO / "Dockerfile"
PYPROJECT = REPO / "pyproject.toml"


def _declared_dependencies() -> list[str]:
    data = tomllib.loads(PYPROJECT.read_text(encoding="utf-8"))
    return data["project"]["dependencies"]


def _distribution_names() -> set[str]:
    """`fastapi>=0.115` / `uvicorn[standard]>=0.30` -> `fastapi` / `uvicorn`。"""
    names = set()
    for spec in _declared_dependencies():
        names.add(re.split(r"[\[<>=!~;\s]", spec, maxsplit=1)[0].strip().lower())
    return names


def _pip_install_commands() -> str:
    """Dockerfile の `pip install` 命令だけを取り出す（継続行を連結）。

    COPY している `alembic.ini` や CMD の `uvicorn` を依存の再掲と誤認しないよう、
    検査対象を「依存をインストールしている場所」に限定する。
    """
    lines = DOCKERFILE.read_text(encoding="utf-8").splitlines()
    commands: list[str] = []
    buffer: list[str] = []
    continuing = False
    for raw in lines:
        line = raw.rstrip()
        if line.lstrip().startswith("#"):
            continue
        if continuing or "pip install" in line:
            buffer.append(line.rstrip("\\").strip())
            continuing = line.endswith("\\")
            if not continuing:
                commands.append(" ".join(buffer))
                buffer = []
    return "\n".join(commands).lower()


def test_dockerfile_does_not_restate_python_dependencies() -> None:
    """Dockerfile の pip install にパッケージ名を書かない。pyproject から入れること。"""
    installed = _pip_install_commands()
    restated = sorted(name for name in _distribution_names() if name in installed)
    assert not restated, (
        "Dockerfile が pyproject.toml の依存を再掲している（AGENTS.md §8）: "
        f"{restated}. 依存は pyproject.toml にだけ書き、Dockerfile は "
        "`pip install .` で取り込むこと。"
    )


def test_dockerfile_installs_the_project_itself() -> None:
    source = DOCKERFILE.read_text(encoding="utf-8")
    assert re.search(r"pip install[^\n]*\s\.(\s|$)", source), (
        "Dockerfile が `pip install .` でプロジェクト自身を入れていない。"
        "依存の単一の宣言元が pyproject.toml でなくなる。"
    )


def test_every_declared_dependency_is_pinned_by_constraints() -> None:
    """再現性: 宣言は pyproject、**版の固定**は constraints.txt（役割を分ける）。"""
    constraints = REPO / "constraints.txt"
    assert constraints.exists(), "constraints.txt が無い（版が固定されない）"

    pinned = {
        re.split(r"[=<>!\[]", line, maxsplit=1)[0].strip().lower()
        for line in constraints.read_text(encoding="utf-8").splitlines()
        if line.strip() and not line.lstrip().startswith("#")
    }
    missing = sorted(_distribution_names() - pinned)
    assert not missing, f"constraints.txt に固定されていない依存: {missing}"
