"""scripts/avp_alerts.py: OpenSearch に依存しない警報口と heartbeat。

理由は docs/testing/opensearch-guard-rationale.md（alerts の節）。
"""

from __future__ import annotations

import importlib.util
import io
import sys
from pathlib import Path
from types import ModuleType

import pytest

ROOT = Path(__file__).resolve().parents[2]


@pytest.fixture(scope="module")
def al() -> ModuleType:
    spec = importlib.util.spec_from_file_location("avp_alerts", ROOT / "scripts" / "avp_alerts.py")
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def test_same_alert_is_not_repeated_until_the_interval_and_resolves_once(
    al: ModuleType, tmp_path: Path
) -> None:
    out = io.StringIO()
    a = al.Alert("K", "crit", "boom")
    al.AlertSink(tmp_path, now=1000.0, repeat_s=3600, stream=out).sync("src", {"K": a})
    al.AlertSink(tmp_path, now=1060.0, repeat_s=3600, stream=out).sync("src", {"K": a})
    assert out.getvalue().count("avp-alert alert") == 1
    al.AlertSink(tmp_path, now=1000.0 + 3600, repeat_s=3600, stream=out).sync("src", {"K": a})
    assert out.getvalue().count("avp-alert repeat") == 1
    al.AlertSink(tmp_path, now=9999.0, repeat_s=3600, stream=out).sync("src", {})
    assert out.getvalue().count("avp-alert resolved") == 1
    assert al.current_alerts(tmp_path) == {}
    assert len((tmp_path / "alerts.jsonl").read_text().splitlines()) == 3


def test_a_level_change_is_emitted_immediately_and_sources_are_independent(
    al: ModuleType, tmp_path: Path
) -> None:
    out = io.StringIO()
    al.AlertSink(tmp_path, now=1.0, stream=out).sync("a", {"K": al.Alert("K", "warn", "m")})
    al.AlertSink(tmp_path, now=2.0, stream=out).sync("b", {"K": al.Alert("K", "crit", "m")})
    al.AlertSink(tmp_path, now=3.0, stream=out).sync("a", {"K": al.Alert("K", "crit", "m")})
    cur = al.current_alerts(tmp_path)
    assert cur["a"]["K"]["level"] == "crit" and cur["a"]["K"]["since"] == 1.0
    assert cur["b"]["K"]["since"] == 2.0
    assert "<2>avp-alert repeat source=a" in out.getvalue()


def test_heartbeat_roundtrip_and_missing_or_corrupt_is_none(al: ModuleType, tmp_path: Path) -> None:
    assert al.heartbeat_age(tmp_path, "x", now=10.0) is None
    al.write_heartbeat(tmp_path, "x", now=4.0, level="ok")
    assert al.heartbeat_age(tmp_path, "x", now=10.0) == 6.0
    (tmp_path / "heartbeat-x.json").write_text("{not json")
    assert al.heartbeat_age(tmp_path, "x", now=10.0) is None
    with pytest.raises(ValueError):
        al.write_heartbeat(tmp_path, "../evil", now=1.0)


def test_unwritable_state_dir_never_raises(al: ModuleType, tmp_path: Path) -> None:
    blocker = tmp_path / "file"
    blocker.write_text("x")
    out = io.StringIO()
    al.AlertSink(blocker / "sub", now=1.0, stream=out).sync("s", {"K": al.Alert("K", "warn", "m")})
    al.write_heartbeat(blocker / "sub", "x", now=1.0)
    assert "avp-alert alert" in out.getvalue()  # journal には出る
