"""新機能 flag を compose.yaml が「読む service にだけ・既定 false で」渡す（R3 / ADR-0080 §A）。

渡し漏れは「ON にできない」、渡し過ぎは「service 間で値が割れる」を生む。
読み手の一覧は ``contracts.release.FEATURE_FLAG_SERVICES``（唯一の宣言元）。
その一覧が実コードの読み手と食い違わないことも、entrypoint の走査で検査する。
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

import yaml

from contracts.release import FEATURE_FLAG_ENV_KEYS, FEATURE_FLAG_SERVICES

REPO = Path(__file__).resolve().parents[2]
COMPOSE = yaml.safe_load((REPO / "compose.yaml").read_text(encoding="utf-8"))

#: flag を読む処理の印 -> flag。entrypoint（service の起動点）にあれば、その service は読み手
READER_MARKERS: dict[str, tuple[str, ...]] = {
    "AVP_QUALITY_GATE_ENFORCE": (
        "quality_gate_enforce",
        "build_quality_service",
        "build_quality_record_writer",
        "require_quality_gate",
    ),
    "BGM_ENABLED": ("bgm_enabled",),
    "GEO_MAPS_ENABLED": ("geo_maps_enabled",),
    "AVP_UPLOAD_DESCRIPTION_COMPOSED": (
        "description_composition_enabled",
        "require_description_composition_for_bgm",
        "build_quality_service",
    ),
}
#: service -> entrypoint（compose の command から導く。apps.api.serve は設定を apps.api 配下で読む）
API_DIR = REPO / "apps" / "api"


def _env(service: str) -> dict[str, Any]:
    env = COMPOSE["services"][service].get("environment", {})
    return dict(env) if isinstance(env, dict) else dict(e.split("=", 1) for e in env)


def test_the_flag_service_map_covers_every_feature_flag() -> None:
    assert set(FEATURE_FLAG_SERVICES) == set(FEATURE_FLAG_ENV_KEYS)
    for key, services in FEATURE_FLAG_SERVICES.items():
        assert services, f"{key}: no reader service"
        assert set(services) <= set(COMPOSE["services"]), key


def test_compose_passes_each_flag_to_exactly_its_reader_services_with_a_false_default() -> None:
    for key, services in FEATURE_FLAG_SERVICES.items():
        passing = {name for name in COMPOSE["services"] if key in _env(name)}
        assert passing == set(services), f"{key}: compose passes it to {sorted(passing)}"
        for name in services:
            assert _env(name)[key] == f"${{{key}:-false}}", f"{name}.{key}"


def _entrypoint_text(service: str) -> str:
    command = COMPOSE["services"][service].get("command") or []
    module = next((c for c in command if re.fullmatch(r"(workers|apps)\.[\w.]+", c)), "")
    if module.startswith("apps.api"):
        return "\n".join(p.read_text(encoding="utf-8") for p in API_DIR.rglob("*.py"))
    path = REPO / (module.replace(".", "/") + ".py")
    return path.read_text(encoding="utf-8") if path.exists() else ""


def test_every_entrypoint_that_reads_a_flag_is_in_the_declared_reader_list() -> None:
    missing: list[str] = []
    for name in COMPOSE["services"]:
        text = _entrypoint_text(name)
        for key, markers in READER_MARKERS.items():
            if any(m in text for m in markers) and name not in FEATURE_FLAG_SERVICES[key]:
                missing.append(f"{name} reads {key}")
    assert not missing, missing
