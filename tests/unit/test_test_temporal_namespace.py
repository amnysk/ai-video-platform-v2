"""integration テストの Temporal namespace 隔離（ADR-0021 追補）。

テストが本番 worker と同じ namespace（``default``）に workflow を起動しないよう、
``TEST_TEMPORAL_NAMESPACE``（既定 ``avp-test``）だけを使い、本番 namespace は収集時に拒否する。
"""

from __future__ import annotations

import pytest

from tests.support.temporal import (
    DEFAULT_TEST_NAMESPACE,
    UnsafeTestTemporalNamespaceError,
    require_test_temporal_namespace,
    validate_test_temporal_namespace,
    worker_subprocess_env,
)


def test_default_is_avp_test(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("TEST_TEMPORAL_NAMESPACE", raising=False)
    monkeypatch.delenv("TEMPORAL_NAMESPACE", raising=False)
    assert DEFAULT_TEST_NAMESPACE == "avp-test"
    assert require_test_temporal_namespace() == "avp-test"


def test_env_override(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("TEST_TEMPORAL_NAMESPACE", "ci-test")
    monkeypatch.delenv("TEMPORAL_NAMESPACE", raising=False)
    assert require_test_temporal_namespace() == "ci-test"


@pytest.mark.parametrize("ns", ["default", "DEFAULT", " default ", ""])
def test_rejects_default_and_empty(ns: str) -> None:
    with pytest.raises(UnsafeTestTemporalNamespaceError):
        validate_test_temporal_namespace(ns, forbidden=())


def test_rejects_production_settings_namespace() -> None:
    with pytest.raises(UnsafeTestTemporalNamespaceError):
        validate_test_temporal_namespace("prod", forbidden=["prod", None])


def test_env_pointing_at_production_is_rejected(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("TEMPORAL_NAMESPACE", "prod-ns")
    monkeypatch.setenv("TEST_TEMPORAL_NAMESPACE", "prod-ns")
    with pytest.raises(UnsafeTestTemporalNamespaceError):
        require_test_temporal_namespace()


def test_worker_env_carries_namespace_not_production(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("TEMPORAL_ADDRESS", "localhost:7233")
    monkeypatch.delenv("TEST_TEMPORAL_NAMESPACE", raising=False)
    monkeypatch.delenv("TEMPORAL_NAMESPACE", raising=False)
    env = worker_subprocess_env()
    assert env == {"TEMPORAL_ADDRESS": "localhost:7233", "TEST_TEMPORAL_NAMESPACE": "avp-test"}
