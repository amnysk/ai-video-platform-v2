"""deploy 前の preflight が API の token ファイルの存在と権限を確かめる（ADR-0080 §B / INV-124）。

token が無いまま API を出すと、変更系は全て 503 になる（fail closed。運用スクリプトも止まる）。
deploy の前に気づくための必須チェック。token そのものは読まず・出力しない。
"""

from __future__ import annotations

import pytest

from domain import release_preflight as rp
from tests.unit.test_release_preflight import Fake, _good_token_probe, _run


def _result(**kw):
    fake = Fake()
    fake.token_probe = _good_token_probe(**kw) if kw else fake.token_probe
    return next(r for r in _run(fake).results if r.name == "api_token_file")


def test_the_check_is_required_before_a_deploy_and_not_after() -> None:
    assert "api_token_file" in rp.PRE_DEPLOY_REQUIRED
    assert "api_token_file" not in rp.POST_DEPLOY_REQUIRED


def test_a_good_file_passes_and_the_detail_names_no_secret() -> None:
    result = _result()
    assert result.status is rp.Status.PASS
    assert "tokens" in result.detail and result.data["actors"] == ["agent", "owner"]


def test_an_absent_file_blocks_the_deploy() -> None:
    result = _result(exists=False, problem="absent", mode=None, uid=None, actors=())
    assert result.status is rp.Status.BLOCKED and "503" in result.detail


@pytest.mark.parametrize(
    "kw",
    [
        {"problem": "readable by group/others (must be 0600)", "mode": 0o644},
        {"problem": "malformed or too short token"},
        {"problem": "not a regular file"},
        {"mode": 0o640},
        {"mode": 0o666},
    ],
)
def test_a_file_the_api_would_reject_blocks_the_deploy(kw) -> None:
    assert _result(**kw).status is rp.Status.BLOCKED


def test_a_file_owned_by_someone_other_than_the_container_user_blocks() -> None:
    result = _result(uid=0, expected_uid=1000)
    assert result.status is rp.Status.BLOCKED and "uid" in result.detail


@pytest.mark.parametrize("actors", [("agent",), ("owner",), ()])
def test_both_an_owner_and_an_agent_token_are_required(actors) -> None:
    assert _result(actors=actors).status is rp.Status.BLOCKED


def test_an_unreadable_probe_is_unknown() -> None:
    fake = Fake()
    fake.token_probe = rp.CollectorUnavailable("permission denied")
    result = next(r for r in _run(fake).results if r.name == "api_token_file")
    assert result.status is rp.Status.UNKNOWN


def test_one_blocked_token_check_makes_the_whole_report_blocked() -> None:
    fake = Fake()
    fake.token_probe = _good_token_probe(exists=False, problem="absent", actors=())
    assert _run(fake).overall is rp.Overall.BLOCKED


# ---- AVP_API_AUTH_MODE=disabled（ADR-0080 追補 2026-11-01）


def test_an_owner_disabled_auth_passes_without_a_token_file() -> None:
    result = _result(
        exists=False, problem="absent", mode=None, uid=None, actors=(), auth_mode="disabled"
    )
    assert result.status is rp.Status.PASS
    assert result.detail == ("api auth disabled by owner decision (AVP_API_AUTH_MODE=disabled)")


@pytest.mark.parametrize("mode", [None, "required", "off", ""])
def test_anything_but_an_explicit_disabled_keeps_the_token_check(mode) -> None:
    result = _result(exists=False, problem="absent", mode=None, uid=None, actors=(), auth_mode=mode)
    assert result.status is rp.Status.BLOCKED
