"""アプリの compose.yaml が、ログ収集の前提（ADR-0040 §1/§3）を満たしていること。

理由は docs/testing/logging-platform-rationale.md §4。
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
import yaml

from contracts.log_contract import (
    APP_LOG_LABEL,
    APP_LOG_LABEL_VALUE,
    ENV_ENVIRONMENT,
    ENV_LOG_FORMAT,
    ENV_SERVICE_NAME,
)

ROOT = Path(__file__).resolve().parents[2]
APP_IMAGES = {"avp2-app:local", "avp2-worker:local"}
REQUIRED_ATTR_LABELS = {"com.docker.compose.service", "com.docker.compose.project", APP_LOG_LABEL}


@pytest.fixture(scope="module")
def services() -> dict[str, Any]:
    return yaml.safe_load((ROOT / "compose.yaml").read_text(encoding="utf-8"))["services"]


def _app_services(services: dict[str, Any]) -> list[str]:
    return [n for n, s in services.items() if s.get("image") in APP_IMAGES]


def _labels(svc: dict[str, Any]) -> dict[str, str]:
    labels = svc.get("labels") or {}
    if isinstance(labels, list):
        return dict(item.split("=", 1) for item in labels)
    return {str(k): str(v) for k, v in labels.items()}


def test_app_services_are_detected(services) -> None:
    apps = _app_services(services)
    assert {"api", "migrate", "dummy-worker", "render-worker", "upload-worker"} <= set(apps)


def test_every_service_rotates_json_file_with_attrs(services) -> None:
    """ローテーション無しの json-file（現状）を残さない。

    attrs が無い行は Collector が project で絞れない。
    """
    for name, svc in services.items():
        logging = svc.get("logging")
        assert logging, f"{name}: logging が無い"
        assert logging["driver"] == "json-file", name
        opts = logging["options"]
        assert opts["max-size"] == "20m" and str(opts["max-file"]) == "5", name
        assert set(opts["labels"].split(",")) == REQUIRED_ATTR_LABELS, name
        assert opts["tag"] == "{{.Name}}", name
        # 非ブロッキングにすると黙って欠損する（ADR-0040 §1）
        assert opts.get("mode", "blocking") == "blocking", name


def test_app_services_are_labelled_and_identify_themselves(services) -> None:
    for name in _app_services(services):
        svc = services[name]
        assert _labels(svc).get(APP_LOG_LABEL) == APP_LOG_LABEL_VALUE, name
        env = svc.get("environment") or {}
        assert env.get(ENV_SERVICE_NAME) == name, f"{name}: service_name は compose のサービス名"
        assert env.get(ENV_ENVIRONMENT) == "${AVP_ENVIRONMENT:-dev}", name
        assert env.get(ENV_LOG_FORMAT) == "${AVP_LOG_FORMAT:-json}", name


def test_infra_services_are_not_labelled_app(services) -> None:
    """postgres・temporal・minio 等の行を app の mapping に入れない。

    unstructured として infra 系統へ流す。
    """
    for name, svc in services.items():
        if name in _app_services(services):
            continue
        assert APP_LOG_LABEL not in _labels(svc), name


def test_api_runs_the_logging_aware_entrypoint(services) -> None:
    """uvicorn の CLI 起動は独自の handler と query 付きの access log を出す（ADR-0040 §1）。"""
    assert services["api"]["command"] == ["python", "-m", "apps.api.serve"]
