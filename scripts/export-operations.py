#!/usr/bin/env python3
"""操作カタログを UI 向けの JSON に書き出す（ADR-0045 §2、INV-52）。

  python scripts/export-operations.py [-o apps/web/static/operations.json]

``contracts.application_operations.OPERATIONS`` から生成する。UI はこの JSON だけを読み、
操作の名前・method・path・scope・人限定・入力を手で再掲しない。差分は
``tests/contract/test_operations_catalog_sync.py`` が検査する。
``paid``（有料の外部生成を伴いうるか）もカタログ（``OperationSpec.paid``）が唯一の宣言元。
**カタログに無い情報は推測して足さない。**
"""

from __future__ import annotations

import argparse
import hashlib
import json
import pathlib
import sys
from typing import Any

REPO = pathlib.Path(__file__).resolve().parents[1]
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))

DEFAULT_OUTPUT = REPO / "apps" / "web" / "static" / "operations.json"
API_PREFIX = "/v1/intervention"


def _type_of(schema: dict[str, Any], defs: dict[str, Any]) -> tuple[str, bool, list[Any] | None]:
    """JSON schema の 1 property → (型の表示, null 可, enum 値)。入れ子は object / array。"""
    if "$ref" in schema:
        return _type_of(defs[schema["$ref"].rsplit("/", 1)[-1]], defs)
    if "anyOf" in schema:
        parts = [_type_of(s, defs) for s in schema["anyOf"]]
        nullable = any(s.get("type") == "null" for s in schema["anyOf"])
        kept = [p for s, p in zip(schema["anyOf"], parts, strict=True) if s.get("type") != "null"]
        names = sorted({p[0] for p in kept})
        enum = next((p[2] for p in kept if p[2] is not None), None)
        return "|".join(names), nullable, enum
    kind = schema.get("type", "object")
    if kind == "string" and "format" in schema:
        kind = f"string:{schema['format']}"
    return str(kind), False, schema.get("enum")


def _fields(model: type[Any] | None) -> list[dict[str, Any]]:
    if model is None:
        return []
    schema = model.model_json_schema()
    defs = schema.get("$defs", {})
    required = set(schema.get("required", []))
    out: list[dict[str, Any]] = []
    for name, prop in schema.get("properties", {}).items():
        type_, nullable, enum = _type_of(prop, defs)
        field: dict[str, Any] = {
            "name": name,
            "type": type_,
            "required": name in required,
            "nullable": nullable,
        }
        if enum is not None:
            field["enum"] = enum
        out.append(field)
    return out


def _path_params(path: str) -> list[str]:
    return [p[1:-1] for p in path.split("/") if p.startswith("{") and p.endswith("}")]


def build() -> dict[str, Any]:
    from contracts.application_operations import OPERATIONS

    operations = [
        {
            "name": spec.name.value,
            "method": spec.http_method,
            "path": API_PREFIX + spec.http_path,
            "path_params": _path_params(spec.http_path),
            "required_scope": spec.required_scope.value,
            "human_only": spec.human_only,
            "paid": spec.paid,
            "effect": spec.effect.value,
            "body_fields": _fields(spec.request_model),
            "query_fields": _fields(spec.query_model),
        }
        for spec in OPERATIONS
    ]
    digest = hashlib.sha256(
        json.dumps(operations, sort_keys=True, ensure_ascii=False).encode("utf-8")
    ).hexdigest()
    return {"catalog_version": digest[:16], "api_prefix": API_PREFIX, "operations": operations}


def render(document: dict[str, Any]) -> str:
    return json.dumps(document, indent=2, sort_keys=True, ensure_ascii=False) + "\n"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="操作カタログを UI 向け JSON に書き出す")
    parser.add_argument("-o", "--output", type=pathlib.Path, default=DEFAULT_OUTPUT)
    args = parser.parse_args(argv)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(render(build()), encoding="utf-8")
    print(f"wrote {args.output}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
