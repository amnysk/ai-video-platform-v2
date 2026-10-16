#!/usr/bin/env python3
"""OpenAPI の断片を書き出す（ADR-0043 §1、INV-52）。

  python scripts/export-openapi.py --application [-o docs/api/openapi.application.json]

``--application``: application 層の path だけと、それが参照する component schema を
``docs/api/openapi.application.json`` に固定する（UI の TypeScript client の生成元。生成は P2）。
差分は ``tests/contract/test_application_contract_sync.py`` が検査する。flag の既定値には依存しない
（route は常に登録され、flag は実行時に見る）。
"""

from __future__ import annotations

import argparse
import json
import pathlib
import sys
from typing import Any

REPO = pathlib.Path(__file__).resolve().parents[1]
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))

DEFAULT_OUTPUT = REPO / "docs" / "api" / "openapi.application.json"


def _refs(node: Any) -> set[str]:
    found: set[str] = set()
    if isinstance(node, dict):
        ref = node.get("$ref")
        if isinstance(ref, str) and ref.startswith("#/components/schemas/"):
            found.add(ref.rsplit("/", 1)[-1])
        for value in node.values():
            found |= _refs(value)
    elif isinstance(node, list):
        for item in node:
            found |= _refs(item)
    return found


def application_openapi() -> dict[str, Any]:
    """application 層の path と、参照される component schema だけの OpenAPI 断片。"""
    from apps.api.main import create_app
    from apps.api.routers import application

    full = create_app().openapi()
    paths = {
        path: item
        for path, item in full["paths"].items()
        if any(route.path == path for route in application.router.routes)  # type: ignore[attr-defined]
    }
    schemas: dict[str, Any] = full.get("components", {}).get("schemas", {})
    needed: set[str] = set()
    queue = list(_refs(paths))
    while queue:
        name = queue.pop()
        if name in needed:
            continue
        needed.add(name)
        queue.extend(_refs(schemas[name]))
    return {
        "openapi": full["openapi"],
        "info": {
            "title": "ai-video-platform-v2 application API",
            "version": full["info"]["version"],
        },
        "paths": paths,
        "components": {"schemas": {name: schemas[name] for name in sorted(needed)}},
    }


def render(document: dict[str, Any]) -> str:
    return json.dumps(document, indent=2, sort_keys=True, ensure_ascii=False) + "\n"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="OpenAPI の断片を書き出す")
    parser.add_argument("--application", action="store_true", required=True)
    parser.add_argument("-o", "--output", type=pathlib.Path, default=DEFAULT_OUTPUT)
    args = parser.parse_args(argv)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(render(application_openapi()), encoding="utf-8")
    print(f"wrote {args.output}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
