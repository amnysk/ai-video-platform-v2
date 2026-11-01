"""RealCollectors が対象環境の flag と API token ファイルを読む（ADR-0080）。token は返さない。"""

from __future__ import annotations

from pathlib import Path

import pytest

from contracts.release import FEATURE_FLAG_ENV_KEYS
from domain.release_preflight import CollectorUnavailable
from infrastructure.release.collectors import RealCollectors

TOKEN_A = "a" * 40
TOKEN_O = "o" * 40


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch: pytest.MonkeyPatch) -> None:
    for key in (
        *FEATURE_FLAG_ENV_KEYS,
        "AVP_UID",
        "AVP_API_SECRETS_DIR",
        "AVP_API_OWNER_TOKEN_PATH",
        "AVP_API_AUTH_MODE",
    ):
        monkeypatch.delenv(key, raising=False)
    monkeypatch.delenv("AVP_API_OWNER_SECRETS_DIR", raising=False)


def _repo(tmp_path: Path, dotenv: str = "") -> Path:
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / ".env").write_text(dotenv)
    return repo


def test_flags_are_read_from_the_repo_dotenv(tmp_path: Path) -> None:
    repo = _repo(tmp_path, "BGM_ENABLED=true\nAVP_QUALITY_GATE_ENFORCE=false\n")
    values = RealCollectors(repo).feature_flag_settings()
    assert set(values) == set(FEATURE_FLAG_ENV_KEYS)
    assert values["BGM_ENABLED"] == "true" and values["AVP_QUALITY_GATE_ENFORCE"] == "false"
    assert values["GEO_MAPS_ENABLED"] is None


def test_a_process_environment_that_differs_from_the_dotenv_is_unknown(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo = _repo(tmp_path, "BGM_ENABLED=false\n")
    monkeypatch.setenv("BGM_ENABLED", "true")  # どちらが効くか確定できない
    with pytest.raises(CollectorUnavailable):
        RealCollectors(repo).feature_flag_settings()
    monkeypatch.setenv("GEO_MAPS_ENABLED", "1")  # .env に無いのに環境にある
    monkeypatch.setenv("BGM_ENABLED", "false")
    with pytest.raises(CollectorUnavailable):
        RealCollectors(repo).feature_flag_settings()


def _write_tokens(directory: Path, text: str, mode: int = 0o600) -> Path:
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / "tokens"
    path.write_text(text)
    path.chmod(mode)
    return path


def test_the_token_probe_reports_state_without_the_tokens(tmp_path: Path) -> None:
    secrets = tmp_path / "secrets"
    path = _write_tokens(secrets, f"owner:{TOKEN_O}\nagent:{TOKEN_A}\n")
    repo = _repo(tmp_path, f"AVP_API_SECRETS_DIR={secrets}\n")
    probe = RealCollectors(repo).api_token_probe()
    assert probe.path == str(path) and probe.exists and probe.problem is None
    assert probe.mode == 0o600 and probe.actors == ("agent", "owner")
    assert probe.expected_uid == 1000 and probe.uid == path.stat().st_uid
    assert TOKEN_A not in repr(probe) and TOKEN_O not in repr(probe)


def test_the_expected_uid_follows_avp_uid(tmp_path: Path) -> None:
    secrets = tmp_path / "secrets"
    _write_tokens(secrets, f"owner:{TOKEN_O}\nagent:{TOKEN_A}\n")
    repo = _repo(tmp_path, f"AVP_API_SECRETS_DIR={secrets}\nAVP_UID=1234\n")
    assert RealCollectors(repo).api_token_probe().expected_uid == 1234


def test_an_absent_file_and_a_loose_file_are_reported_as_problems(tmp_path: Path) -> None:
    repo = _repo(tmp_path, f"AVP_API_SECRETS_DIR={tmp_path / 'none'}\n")
    probe = RealCollectors(repo).api_token_probe()
    assert probe.exists is False and probe.problem == "absent"
    loose = tmp_path / "loose"
    _write_tokens(loose, f"owner:{TOKEN_O}\nagent:{TOKEN_A}\n", mode=0o644)
    other = tmp_path / "other"
    other.mkdir()
    (other / ".env").write_text(f"AVP_API_SECRETS_DIR={loose}\n")
    probe = RealCollectors(other).api_token_probe()
    assert probe.problem is not None and "0600" in probe.problem and probe.mode == 0o644


def test_a_separate_owner_file_is_probed_when_configured(tmp_path: Path) -> None:
    shared, owner_dir = tmp_path / "shared", tmp_path / "ownerdir"
    _write_tokens(shared, f"agent:{TOKEN_A}\n")
    owner_dir.mkdir()
    owner_file = owner_dir / "owner-token"
    owner_file.write_text(f"owner:{TOKEN_O}\n")
    owner_file.chmod(0o600)
    repo = _repo(
        tmp_path,
        f"AVP_API_SECRETS_DIR={shared}\nAVP_API_OWNER_SECRETS_DIR={owner_dir}\n"
        "AVP_API_OWNER_TOKEN_PATH=/run/avp-secrets/api-owner/owner-token\n",
    )
    probe = RealCollectors(repo).api_token_probe()
    assert probe.problem is None and probe.actors == ("agent", "owner")
    owner_file.chmod(0o644)
    assert RealCollectors(repo).api_token_probe().problem is not None


def test_the_probe_reads_the_auth_mode_from_the_dotenv(tmp_path: Path) -> None:
    repo = _repo(tmp_path, "AVP_API_AUTH_MODE=disabled\n")
    assert RealCollectors(repo).api_token_probe().auth_mode == "disabled"
    (tmp_path / "x").mkdir()
    assert RealCollectors(_repo(tmp_path / "x")).api_token_probe().auth_mode is None


def test_an_auth_mode_in_the_process_environment_that_differs_from_the_dotenv_is_unknown(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo = _repo(tmp_path, "AVP_API_AUTH_MODE=disabled\n")
    monkeypatch.setenv("AVP_API_AUTH_MODE", "required")
    with pytest.raises(CollectorUnavailable):
        RealCollectors(repo).api_token_probe()


def test_a_matching_process_environment_is_accepted(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo = _repo(tmp_path, "AVP_API_AUTH_MODE=disabled\n")
    monkeypatch.setenv("AVP_API_AUTH_MODE", "disabled")
    assert RealCollectors(repo).api_token_probe().auth_mode == "disabled"
