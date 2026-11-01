"""integration テストが本番スタックへ繋がる事故を、収集時に止める規則（ADR-0079 / INV-121）。"""

from __future__ import annotations

import pytest

from tests.support.production_guard import (
    ENV_ISOLATION,
    ProductionTargetError,
    assert_isolated_targets,
)

LOCAL = frozenset({"192.168.1.9", "172.17.0.1"})


def _resolver(mapping: dict[str, list[str]]):
    return lambda host: mapping.get(host, [])


def _check(env: dict[str, str], *, resolve: dict[str, list[str]] | None = None) -> None:
    assert_isolated_targets(env, local_addresses=LOCAL, resolver=_resolver(resolve or {}))


APPTEST = {
    ENV_ISOLATION: "apptest:avp2-oslog-c-p3a",
    "TEMPORAL_ADDRESS": "temporal:7233",
    "TEST_DATABASE_URL": "postgresql+psycopg://avp:x@postgres:5432/avp_test",
    "DATABASE_URL": "postgresql+psycopg://avp:x@postgres:5432/avp",
    "MINIO_ENDPOINT": "http://minio:9000",
}
CI = {
    ENV_ISOLATION: "ci-ephemeral",
    "GITHUB_ACTIONS": "true",
    "TEMPORAL_ADDRESS": "localhost:7233",
    "TEST_DATABASE_URL": "postgresql+psycopg://avp:x@localhost:5432/avp_test",
    "MINIO_ENDPOINT": "http://localhost:9000",
}


def test_no_targets_configured_is_not_checked() -> None:
    _check({})


def test_the_isolated_apptest_stack_passes() -> None:
    _check(APPTEST, resolve={"temporal": ["172.20.0.2"], "postgres": ["172.20.0.3"]})


def test_the_ci_ephemeral_services_pass() -> None:
    _check(CI, resolve={"localhost": ["127.0.0.1"]})


@pytest.mark.parametrize(
    "env",
    [
        {"TEMPORAL_ADDRESS": "localhost:7233"},
        {"TEST_DATABASE_URL": "postgresql+psycopg://avp:x@localhost:5432/avp_test"},
        {"MINIO_ENDPOINT": "http://127.0.0.1:9000"},
        {"TEMPORAL_ADDRESS": "192.168.1.9:7233"},
    ],
)
def test_a_target_without_an_isolation_declaration_is_refused(env: dict[str, str]) -> None:
    with pytest.raises(ProductionTargetError):
        _check(env)


@pytest.mark.parametrize(
    "declared",
    ["", "ci-ephemeral ", "CI-ephemeral", "ci_ephemeral", "1", "true", "apptest", "apptest:avp2"],
)
def test_a_mistyped_declaration_fails_closed(declared: str) -> None:
    env = {**CI, ENV_ISOLATION: declared}
    with pytest.raises(ProductionTargetError):
        _check(env, resolve={"localhost": ["127.0.0.1"]})


def test_the_ci_declaration_is_refused_outside_github_actions() -> None:
    env = {k: v for k, v in CI.items() if k != "GITHUB_ACTIONS"}
    with pytest.raises(ProductionTargetError):
        _check(env, resolve={"localhost": ["127.0.0.1"]})


@pytest.mark.parametrize(
    "override",
    [
        {"TEMPORAL_ADDRESS": "localhost:7233"},
        {"TEMPORAL_ADDRESS": "192.168.1.9:7233"},
        {"TEST_DATABASE_URL": "postgresql+psycopg://avp:x@localhost:5432/avp_test"},
        {"MINIO_ENDPOINT": "http://localhost:9000"},
        {"MINIO_ENDPOINT": "http://host.docker.internal:9000"},
    ],
)
def test_the_apptest_declaration_never_allows_the_host_or_its_published_ports(
    override: dict[str, str],
) -> None:
    env = {**APPTEST, **override}
    with pytest.raises(ProductionTargetError):
        _check(
            env,
            resolve={
                "localhost": ["127.0.0.1"],
                "host.docker.internal": ["172.17.0.1"],
                "temporal": ["172.20.0.2"],
                "postgres": ["172.20.0.3"],
            },
        )


def test_an_unknown_hostname_that_resolves_to_this_host_is_refused() -> None:
    env = {**CI, ENV_ISOLATION: "apptest:avp2-oslog-c", "TEMPORAL_ADDRESS": "tmp.lan:7233"}
    del env["GITHUB_ACTIONS"]
    with pytest.raises(ProductionTargetError):
        _check(env, resolve={"tmp.lan": ["192.168.1.9"]})


def test_the_production_compose_project_is_never_a_valid_declaration() -> None:
    env = {**APPTEST, ENV_ISOLATION: "apptest:avp2"}
    with pytest.raises(ProductionTargetError):
        _check(env)


def test_ci_must_not_reach_a_non_loopback_host() -> None:
    env = {**CI, "TEMPORAL_ADDRESS": "temporal.example.net:7233"}
    with pytest.raises(ProductionTargetError):
        _check(env, resolve={"localhost": ["127.0.0.1"]})


def test_an_unparsable_target_fails_closed() -> None:
    with pytest.raises(ProductionTargetError):
        _check({**APPTEST, "TEMPORAL_ADDRESS": "not-an-address"})
