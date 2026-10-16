"""storyboard の input_hash（ADR-0012 / ADR-0015）。"""

from __future__ import annotations

import inspect

import pytest

from domain.script import identity as script_identity
from domain.storyboard import identity
from domain.storyboard.identity import storyboard_input_hash

_BASE = {
    "episode_id": "ep-1",
    "artifact_type": "storyboard",
    "target_schema_version": "1.0",
    "script_sha256": "a" * 64,
    "prompt_template_id": "storyboard_ja",
    "prompt_template_version": "1",
    "generator_id": "gen:model",
    "generation_spec_id": "spec-1",
}


def test_hash_is_deterministic() -> None:
    assert storyboard_input_hash(**_BASE) == storyboard_input_hash(**dict(_BASE))


@pytest.mark.parametrize("field", sorted(_BASE))
def test_every_included_field_changes_the_hash(field: str) -> None:
    changed = {**_BASE, field: _BASE[field] + "-changed"}
    assert storyboard_input_hash(**changed) != storyboard_input_hash(**_BASE)


def test_excluded_fields_are_not_parameters() -> None:
    """ラウンド・試行・job_id・時刻・run id は構造的に混ぜられない（呼び出し毎に変わる値）。"""
    params = set(inspect.signature(storyboard_input_hash).parameters)
    assert params == set(_BASE)
    for excluded in ("round", "attempt", "job_id", "timestamp", "run_id", "workflow_run_id"):
        assert excluded not in params
        with pytest.raises(TypeError):
            storyboard_input_hash(**_BASE, **{excluded: "x"})


def test_idempotency_key_is_reused_not_duplicated() -> None:
    assert identity.idempotency_key is script_identity.idempotency_key
