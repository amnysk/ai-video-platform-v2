"""compose が API の token ファイルを渡す（ADR-0080 §B / INV-124）。

M4（独立レビュー）: AVP_API_TOKENS_PATH が compose に無く、デプロイしても設定の手段が無かった。
ファイルが無くても compose は起動し、API は変更系を全拒否する（fail closed）ことを前提にしている。
"""

from __future__ import annotations

from pathlib import Path

import yaml

from infrastructure.config import Settings

ROOT = Path(__file__).resolve().parents[2]


def _api() -> dict:
    return yaml.safe_load((ROOT / "compose.yaml").read_text(encoding="utf-8"))["services"]["api"]


def test_the_api_gets_the_token_file_path_the_settings_read() -> None:
    env = _api()["environment"]
    alias = Settings.model_fields["api_tokens_file"].validation_alias
    assert env[alias] == "/run/avp-secrets/api/tokens"
    assert Settings.model_fields["api_owner_token_file"].validation_alias in env


def test_the_secret_directory_is_mounted_read_only_with_a_safe_default() -> None:
    mounts = [v for v in _api()["volumes"] if v["target"] == "/run/avp-secrets/api"]
    assert len(mounts) == 1
    mount = mounts[0]
    assert mount["type"] == "bind" and mount["read_only"] is True
    # 既定は repo の外（git の dirty 判定・誤コミットを避ける）。環境変数で変えられる
    assert mount["source"] == "${AVP_API_SECRETS_DIR:-${HOME}/.config/avp2/api}"


def test_no_token_value_is_written_into_a_deployment_file() -> None:
    for name in ("compose.yaml", "deploy/api-owner-token.compose.yaml"):
        text = (ROOT / name).read_text(encoding="utf-8")
        assert "Bearer" not in text and "owner:" not in text


def test_the_owner_file_override_mounts_a_separate_directory() -> None:
    override = yaml.safe_load(
        (ROOT / "deploy/api-owner-token.compose.yaml").read_text(encoding="utf-8")
    )
    (mount,) = override["services"]["api"]["volumes"]
    assert mount["target"] == "/run/avp-secrets/api-owner" and mount["read_only"] is True
    assert mount["source"] != _api()["volumes"][0]["source"]


def test_the_api_port_binding_is_unchanged_by_this_branch() -> None:
    assert _api()["ports"] == ["8000:8000"]


def test_the_release_runbook_has_the_token_setup_step_and_names_the_check() -> None:
    runbook = (ROOT / "docs/operations/release-runbook.md").read_text(encoding="utf-8")
    assert "api_token_file" in runbook and "api-auth.md" in runbook
    assert "enables" in runbook and "BGM_ENABLED" in runbook
