"""RealCollectors.target_info は cwd ではなく repo root の .env を読む（DB には接続しない）。"""

from __future__ import annotations

from pathlib import Path

import pytest

from infrastructure.release.collectors import RealCollectors


def test_target_info_reads_the_repo_root_dotenv_not_the_cwd(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo, elsewhere = tmp_path / "repo", tmp_path / "cwd"
    repo.mkdir()
    elsewhere.mkdir()
    (repo / ".env").write_text(
        "DATABASE_URL=postgresql+psycopg://u:secret@prod-db:5432/avp\n"
        "TEMPORAL_NAMESPACE=default\nAVP_ENVIRONMENT=production\n"
    )
    (elsewhere / ".env").write_text(
        "DATABASE_URL=postgresql+psycopg://u:p@stage-db:5432/avp_staging\n"
        "TEMPORAL_NAMESPACE=avp-staging\n"
    )
    for key in ("DATABASE_URL", "TEMPORAL_NAMESPACE", "AVP_ENVIRONMENT", "TEMPORAL_ADDRESS"):
        monkeypatch.delenv(key, raising=False)
    monkeypatch.chdir(elsewhere)
    info = RealCollectors(repo).target_info()
    assert (info.db_host, info.db_name) == ("prod-db", "avp")
    assert info.temporal_namespace == "default"
    assert info.environment == "production"
    assert "secret" not in repr(info)
