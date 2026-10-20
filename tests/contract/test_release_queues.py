"""preflight が見る task queue は compose.yaml の healthcheck と一致する（AGENTS.md §8）。"""

from __future__ import annotations

from pathlib import Path

import yaml

from domain.release_preflight import APP_IMAGES, REVISION_LABEL
from infrastructure.release.compose_queues import required_queues

ROOT = Path(__file__).resolve().parents[2]


def test_queue_list_equals_the_yaml_parsed_healthcheck_queues() -> None:
    data = yaml.safe_load((ROOT / "compose.yaml").read_text(encoding="utf-8"))
    expected: set[str] = set()
    for svc in data["services"].values():
        test = svc.get("healthcheck", {}).get("test", [])
        for i, arg in enumerate(test):
            if arg == "--queue":
                expected.add(test[i + 1])
    got = required_queues((ROOT / "compose.yaml").read_text(encoding="utf-8"))
    assert set(got) == expected and expected


def test_app_images_and_label_match_compose_and_the_existing_scripts() -> None:
    data = yaml.safe_load((ROOT / "compose.yaml").read_text(encoding="utf-8"))
    built = {s["image"] for s in data["services"].values() if "build" in s}
    assert built <= set(APP_IMAGES)
    versions = (ROOT / "scripts" / "workers-versions.sh").read_text(encoding="utf-8")
    assert all(image in versions for image in APP_IMAGES)
    assert REVISION_LABEL in versions
