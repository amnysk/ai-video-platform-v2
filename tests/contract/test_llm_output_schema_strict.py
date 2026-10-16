"""LLM に渡す出力スキーマは OpenAI structured output の strict 形式を満たす。

codex exec --output-schema は strict で検証する: 全 object で ``required`` が全 property を含み、
``additionalProperties`` が false。
満たさないと本番で invalid_json_schema になる（2026-09-19 smoke）。
"""

from __future__ import annotations

from typing import Any

import pytest

from contracts.artifacts import ScriptArtifact
from contracts.topic_planning import TopicCandidateBatch


def _objects(schema: dict[str, Any]) -> list[tuple[str, dict[str, Any]]]:
    found = [("root", schema)] if schema.get("type") == "object" else []
    found += [(n, d) for n, d in schema.get("$defs", {}).items() if d.get("type") == "object"]
    return found


@pytest.mark.parametrize("model", [TopicCandidateBatch, ScriptArtifact])
def test_every_object_requires_all_properties_and_forbids_extras(model: Any) -> None:
    for name, obj in _objects(model.model_json_schema()):
        missing = set(obj["properties"]) - set(obj.get("required", []))
        assert not missing, f"{model.__name__}.{name}: not required {sorted(missing)}"
        assert obj.get("additionalProperties") is False, f"{model.__name__}.{name}"
