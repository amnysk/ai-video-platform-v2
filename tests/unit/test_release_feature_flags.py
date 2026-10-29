"""release 承認と対象環境の新機能 flag の突き合わせ（ADR-0080 §A / INV-123）。

承認記録の ``enables`` は自己申告で任意だった（委任された承認が書かなければ通った）。
今は必須で、preflight が対象環境の実際の値と比べる。
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from typing import Any

import pytest

from contracts.release import FEATURE_FLAG_ENV_KEYS
from domain import release_preflight as rp
from infrastructure.config import Settings
from tests.unit.test_release_preflight import Fake, _approval, _run

DELEGATED = "AGENT-20261029-deploy01:OWNER-20261029-instr01"


def _approval_result(extra: dict[str, Any], flags: Mapping[str, str | None] | None = None):
    fake = Fake()
    fake.flags = dict(flags or {})
    fake.texts = {**fake.texts, "approval.json": _approval(extra=extra)}
    return next(r for r in _run(fake).results if r.name == "owner_approval")


def test_the_flag_list_is_the_four_new_features_and_each_is_a_default_off_setting() -> None:
    assert set(FEATURE_FLAG_ENV_KEYS) == {
        "AVP_QUALITY_GATE_ENFORCE",
        "BGM_ENABLED",
        "GEO_MAPS_ENABLED",
        "AVP_UPLOAD_DESCRIPTION_COMPOSED",
    }
    from contracts.upload import UPLOAD_DESCRIPTION_COMPOSITION_ENV, description_composition_enabled

    by_env = {
        (f.validation_alias if isinstance(f.validation_alias, str) else name.upper()): f
        for name, f in Settings.model_fields.items()
    }
    for key in FEATURE_FLAG_ENV_KEYS:
        if key == UPLOAD_DESCRIPTION_COMPOSITION_ENV:  # Settings を通らず env を直接読む唯一の flag
            assert description_composition_enabled({}) is False
            continue
        assert key in by_env, f"{key} is not a Settings key"
        assert by_env[key].default is False


def test_enables_is_mandatory_even_for_an_owner_approval() -> None:
    doc = json.loads(_approval())
    del doc["enables"]
    fake = Fake()
    fake.texts = {**fake.texts, "approval.json": json.dumps(doc)}
    result = next(r for r in _run(fake).results if r.name == "owner_approval")
    assert result.status is rp.Status.UNKNOWN and "enables" in result.detail


def test_an_explicit_empty_enables_is_a_flags_off_deploy() -> None:
    assert _approval_result({"approval_ref": DELEGATED, "enables": []}).status is rp.Status.PASS


@pytest.mark.parametrize("key", FEATURE_FLAG_ENV_KEYS)
@pytest.mark.parametrize("value", ["true", "True", "1", "yes", "on", "enabled", "2"])
def test_a_delegated_approval_is_blocked_when_any_flag_is_on_in_the_target(
    key: str, value: str
) -> None:
    result = _approval_result({"approval_ref": DELEGATED, "enables": []}, {key: value})
    assert result.status is rp.Status.BLOCKED
    assert key in result.detail


@pytest.mark.parametrize("value", [None, "", "false", "False", "0", "no", "off", " false "])
def test_flags_that_are_off_or_unset_do_not_block_a_delegated_approval(value: str | None) -> None:
    flags = dict.fromkeys(FEATURE_FLAG_ENV_KEYS, value)
    assert _approval_result({"approval_ref": DELEGATED, "enables": []}, flags).status is (
        rp.Status.PASS
    )


def test_a_delegated_approval_that_declares_enabling_a_feature_is_still_blocked() -> None:
    result = _approval_result(
        {"approval_ref": DELEGATED, "enables": ["enable_new_feature"]}, {"BGM_ENABLED": "true"}
    )
    assert result.status is rp.Status.BLOCKED


def test_an_owner_approval_with_a_flag_on_must_declare_enable_new_feature() -> None:
    flags = {"BGM_ENABLED": "true"}
    undeclared = _approval_result({"enables": []}, flags)
    assert undeclared.status is rp.Status.BLOCKED and "BGM_ENABLED" in undeclared.detail
    declared = _approval_result({"enables": ["enable_new_feature"]}, flags)
    assert declared.status is rp.Status.PASS
    assert declared.data["flags_on"] == ["BGM_ENABLED"]


def test_an_unreadable_environment_is_unknown_never_pass() -> None:
    fake = Fake()
    fake.flags = rp.CollectorUnavailable("process env differs from .env")
    result = next(r for r in _run(fake).results if r.name == "owner_approval")
    assert result.status is rp.Status.UNKNOWN
