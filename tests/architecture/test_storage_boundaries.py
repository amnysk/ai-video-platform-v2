"""MinIO のデータディレクトリをアプリコードから直接触らない（ADR-0016）。

正式な成果物の経路は ArtifactStore → MinIO API だけ。検査対象は**コード層だけ**に限定する
（docs / CI / compose はマウント設定として正当に参照する）。
"""

from __future__ import annotations

import pathlib

REPO = pathlib.Path(__file__).resolve().parents[2]
CODE_LAYERS = ("apps", "workers", "domain", "infrastructure", "contracts", "prompts", "scripts")

#: 禁止ディレクトリを**拒否するために**定義する唯一の場所。
ALLOWED = {"infrastructure/workdir.py"}

NEEDLES = ("minio-data",)


def test_code_layers_do_not_reference_the_minio_data_directory() -> None:
    violations: list[str] = []
    for layer in CODE_LAYERS:
        for path in sorted((REPO / layer).rglob("*")):
            if not path.is_file() or "__pycache__" in path.parts:
                continue
            rel = path.relative_to(REPO).as_posix()
            if rel in ALLOWED:
                continue
            try:
                text = path.read_text(encoding="utf-8")
            except UnicodeDecodeError:
                continue
            if any(needle in text for needle in NEEDLES):
                violations.append(rel)
    assert not violations, f"MinIO のデータディレクトリはMinIOだけが扱う: {violations}"
