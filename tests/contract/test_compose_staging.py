"""compose.staging.yaml が本番（compose.yaml）と何も共有しないことの検査（ADR-0049 / INV-65）。

staging を本番と同じホストで動かすので、1行の共有（volume・bind・port・namespace・DB・資格情報・
イメージタグ）が本番データや課金に直結する。ここで機械的に固定する。
各テストの理由: docs/testing/release-preflight-rationale.md。
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

import yaml

ROOT = Path(__file__).resolve().parents[2]
PROD = yaml.safe_load((ROOT / "compose.yaml").read_text(encoding="utf-8"))
STG_TEXT = (ROOT / "compose.staging.yaml").read_text(encoding="utf-8")
STG = yaml.safe_load(STG_TEXT)
PROD_TEXT = (ROOT / "compose.yaml").read_text(encoding="utf-8")


def _env(service: dict[str, Any]) -> dict[str, str]:
    env = service.get("environment", {})
    if isinstance(env, list):
        env = dict(item.split("=", 1) for item in env)
    return {k: "" if v is None else str(v) for k, v in env.items()}


def _volume_entries(doc: dict[str, Any]) -> list[tuple[str, str]]:
    """(source, target) の一覧。short / long syntax の両方。"""
    out: list[tuple[str, str]] = []
    for svc in doc["services"].values():
        for vol in svc.get("volumes", []):
            if isinstance(vol, str):
                parts = vol.split(":")
                out.append((parts[0], parts[1] if len(parts) > 1 else ""))
            else:
                out.append((str(vol.get("source", "")), str(vol.get("target", ""))))
    return out


def _host_ports(doc: dict[str, Any]) -> list[int]:
    ports: list[int] = []
    for svc in doc["services"].values():
        for spec in svc.get("ports", []):
            parts = str(spec).split(":")
            ports.append(int(parts[-2]))  # host:container / ip:host:container
    return ports


def test_project_name_differs_from_production() -> None:
    assert STG["name"] == "avp2-staging"
    assert STG["name"] != PROD["name"]


def test_no_volume_or_bind_path_is_shared_with_production() -> None:
    prod_sources = {src for src, _ in _volume_entries(PROD)}
    stg_sources = {src for src, _ in _volume_entries(STG)}
    assert not prod_sources & stg_sources, prod_sources & stg_sources
    assert not set(PROD.get("volumes", {})) & set(STG.get("volumes", {}))


def test_staging_never_bind_mounts_a_host_path() -> None:
    """named volume だけ。host path の bind mount は不可（本番の MinIO 実データ置き場を含む）。"""
    for source, target in _volume_entries(STG):
        assert not source.startswith(("/", ".", "~", "$")), f"bind mount: {source}:{target}"
    assert "/mnt/minio-hdd" not in STG_TEXT
    assert "docker.sock" not in STG_TEXT


def test_every_staging_volume_source_is_a_declared_named_volume() -> None:
    declared = set(STG.get("volumes", {}))
    for source, _ in _volume_entries(STG):
        assert source in declared, source


def test_no_host_port_collides_with_production() -> None:
    stg, prod = _host_ports(STG), _host_ports(PROD)
    assert stg and len(stg) == len(set(stg)), "staging 内でも host port が重複しない"
    assert not set(stg) & set(prod), set(stg) & set(prod)


def test_staging_ports_listen_on_loopback_only() -> None:
    for svc in STG["services"].values():
        for spec in svc.get("ports", []):
            assert str(spec).startswith("127.0.0.1:"), spec


def test_temporal_namespace_is_not_the_default_everywhere() -> None:
    seen = 0
    for name, svc in STG["services"].items():
        env = _env(svc)
        if "TEMPORAL_NAMESPACE" in env:
            seen += 1
            assert env["TEMPORAL_NAMESPACE"] == "avp-staging", name
    assert seen >= 3
    assert _env(STG["services"]["temporal"])["DEFAULT_NAMESPACE"] == "avp-staging"


def test_paid_provider_and_youtube_credentials_are_empty_and_uploads_are_off() -> None:
    for name, svc in STG["services"].items():
        for key, value in _env(svc).items():
            if re.search(r"FAL|YOUTUBE", key):
                expected = "false" if key.endswith("_ENABLED") else ""
                assert value == expected, f"{name}.{key} must be {expected!r} in staging"
    assert STG_TEXT.count("FAL_KEY") >= 1  # 「持たない」ではなく「空と明示する」
    for name in ("api", "pipeline-worker"):
        env = _env(STG["services"][name])
        assert env["UPLOADS_PAUSED"] == "true", name
        assert env["PAUSED"] == "true", name


def test_no_upload_or_paid_worker_is_defined() -> None:
    forbidden = ("upload", "image", "video", "voice", "script", "storyboard", "alternative")
    for name in STG["services"]:
        assert not any(w in name for w in forbidden), name


def test_no_production_secret_can_leak_in_through_interpolation() -> None:
    """``${VAR}`` は本番の .env を読む。staging は全て literal にする。"""
    assert "${" not in STG_TEXT
    for svc in STG["services"].values():
        assert "env_file" not in svc


def test_database_minio_and_images_are_separate_from_production() -> None:
    stg_db = _env(STG["services"]["migrate"])["DATABASE_URL"]
    assert stg_db.endswith("@postgres:5432/avp_staging")
    assert "/avp" not in stg_db.replace("avp_staging", "").replace("avp-staging", "")[-6:]
    assert _env(STG["services"]["minio"])["MINIO_ROOT_USER"] != "minioadmin"
    buckets = {_env(s).get("MINIO_BUCKET") for s in STG["services"].values()} - {None}
    assert buckets == {"artifacts-staging"}
    prod_images = {s["image"] for s in PROD["services"].values() if "image" in s}
    for name, svc in STG["services"].items():
        if "build" in svc:
            assert svc["image"] not in prod_images, f"{name}: 本番のタグを上書きする"
            assert svc["image"].startswith("avp2-staging-")


def test_staging_logs_are_not_picked_up_as_production_app_logs() -> None:
    assert "avp.logging" not in STG_TEXT
    for name, svc in STG["services"].items():
        if "AVP_ENVIRONMENT" in _env(svc):
            assert _env(svc)["AVP_ENVIRONMENT"] == "staging", name


def test_container_names_and_networks_are_not_shared() -> None:
    assert "container_name" not in PROD_TEXT or "container_name" not in STG_TEXT
    for svc in STG["services"].values():
        assert svc.get("network_mode") != "host"
        assert "container_name" not in svc
