"""Codex の model 設定は compose の 1 箇所から全 worker へ渡る（ADR-0058 / AGENTS.md §8 / INV-75）。

3 つの worker（script / storyboard / scene-alternative）が別々に ``CODEX_MODEL`` を書くと、
ある worker だけ model が違う、という事故になる（片方だけ直して残りが古いまま）。
"""

from __future__ import annotations

import pathlib

import yaml

REPO = pathlib.Path(__file__).resolve().parents[2]
CODEX_KEYS = (
    "CODEX_BINARY",
    "CODEX_MODEL",
    "CODEX_ALLOWED_MODELS",
    "CODEX_REQUIRE_PINNED_MODEL",
    "CODEX_TIMEOUT_SECONDS",
)


def _compose() -> dict:
    return yaml.safe_load((REPO / "compose.yaml").read_text(encoding="utf-8"))


def test_every_codex_worker_gets_the_same_model_settings() -> None:
    services = _compose()["services"]
    codex_services = {
        name: svc["environment"]
        for name, svc in services.items()
        if "CODEX_BINARY" in (svc.get("environment") or {})
    }
    assert set(codex_services) == {"script-worker", "storyboard-worker", "scene-alternative-worker"}
    reference = {k: codex_services["script-worker"][k] for k in CODEX_KEYS}
    for name, env in codex_services.items():
        assert {k: env.get(k) for k in CODEX_KEYS} == reference, name


def test_the_model_setting_is_declared_once_in_compose() -> None:
    text = (REPO / "compose.yaml").read_text(encoding="utf-8")
    for key in ("CODEX_MODEL", "CODEX_ALLOWED_MODELS", "CODEX_REQUIRE_PINNED_MODEL"):
        assert text.count(f"{key}: ") == 1, f"{key} must be declared once (x-codex-env)"
