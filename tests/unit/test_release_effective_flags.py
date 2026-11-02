"""preflight の flag 値は compose が解決した実効値でも確かめる（R3）。

`.env` が OFF でも、compose の override（literal の ON）や service ごとの食い違いは container では
ON になりうる。解決後の environment（``docker compose config``）を注入して、オフラインで検査する。
"""

from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path

import pytest

from contracts.release import FEATURE_FLAG_ENV_KEYS
from domain.release_preflight import CollectorUnavailable
from infrastructure.release.collectors import RealCollectors


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch: pytest.MonkeyPatch) -> None:
    for key in FEATURE_FLAG_ENV_KEYS:
        monkeypatch.delenv(key, raising=False)


def _collectors(
    tmp_path: Path, dotenv: str, resolved: Mapping[str, Mapping[str, str]]
) -> RealCollectors:
    repo = tmp_path / f"repo{len(list(tmp_path.iterdir()))}"
    repo.mkdir()
    (repo / ".env").write_text(dotenv)
    return RealCollectors(repo, compose_environment=lambda: resolved)


def test_a_consistent_resolution_is_returned(tmp_path: Path) -> None:
    resolved = {
        "render-worker": {"BGM_ENABLED": "true"},
        "upload-worker": {"BGM_ENABLED": "true", "AVP_UPLOAD_DESCRIPTION_COMPOSED": "false"},
    }
    values = _collectors(tmp_path, "BGM_ENABLED=true\n", resolved).feature_flag_settings()
    assert values["BGM_ENABLED"] == "true"
    assert values["AVP_UPLOAD_DESCRIPTION_COMPOSED"] == "false"
    assert values["GEO_MAPS_ENABLED"] is None


def test_a_compose_override_that_turns_a_flag_on_while_dotenv_is_off_is_unknown(
    tmp_path: Path,
) -> None:
    resolved = {"render-worker": {"GEO_MAPS_ENABLED": "true"}}
    with pytest.raises(CollectorUnavailable, match="GEO_MAPS_ENABLED"):
        _collectors(tmp_path, "GEO_MAPS_ENABLED=false\n", resolved).feature_flag_settings()
    with pytest.raises(CollectorUnavailable, match="GEO_MAPS_ENABLED"):
        _collectors(tmp_path, "", resolved).feature_flag_settings()  # .env に無い


def test_services_that_resolve_the_same_flag_differently_are_unknown(tmp_path: Path) -> None:
    resolved = {
        "render-worker": {"BGM_ENABLED": "true"},
        "upload-worker": {"BGM_ENABLED": "false"},
    }
    with pytest.raises(CollectorUnavailable, match="BGM_ENABLED"):
        _collectors(tmp_path, "BGM_ENABLED=true\n", resolved).feature_flag_settings()


def test_spelling_differences_between_equal_meanings_are_not_a_conflict(tmp_path: Path) -> None:
    resolved = {"render-worker": {"BGM_ENABLED": "1"}, "upload-worker": {"BGM_ENABLED": "true"}}
    values = _collectors(tmp_path, "BGM_ENABLED=true\n", resolved).feature_flag_settings()
    assert values["BGM_ENABLED"] in {"1", "true"}


def test_a_failing_compose_resolution_is_unknown(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / ".env").write_text("")

    def boom() -> Mapping[str, Mapping[str, str]]:
        raise CollectorUnavailable("docker compose config exited 1")

    with pytest.raises(CollectorUnavailable):
        RealCollectors(repo, compose_environment=boom).feature_flag_settings()


def test_the_acquisition_flag_is_reported_from_dotenv_and_unset_is_none(tmp_path: Path) -> None:
    """INV-159: 新しい flag も release record に載る（.env の値。未設定は None = OFF 扱い）。"""
    on = _collectors(tmp_path, "BGM_ACQUISITION_ENABLED=true\n", {}).feature_flag_settings()
    assert on["BGM_ACQUISITION_ENABLED"] == "true"
    unset = _collectors(tmp_path, "", {}).feature_flag_settings()
    assert unset["BGM_ACQUISITION_ENABLED"] is None


def test_a_literal_acquisition_on_in_compose_is_unknown_not_ready(tmp_path: Path) -> None:
    resolved = {"bgm-acquisition-worker": {"BGM_ACQUISITION_ENABLED": "true"}}
    with pytest.raises(CollectorUnavailable, match="BGM_ACQUISITION_ENABLED"):
        _collectors(tmp_path, "BGM_ACQUISITION_ENABLED=false\n", resolved).feature_flag_settings()
