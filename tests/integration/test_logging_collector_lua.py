"""Collector（Fluent Bit + Lua）の振り分け・型修復・安全化を、採用版のイメージで実際に通す。

本物の ``deploy/logging/fluent-bit/fluent-bit.yaml`` の filter 列をそのまま使い、入力を
Docker json-file 形式の一時ファイル、出力を stdout に差し替える。OpenSearch は使わない。理由は
docs/testing/logging-platform-rationale.md §3。docker と Fluent Bit のイメージが無ければ skip。
"""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path
from typing import Any

import pytest
import yaml

from contracts.log_contract import (
    APP_LOG_LABEL,
    APP_LOG_LABEL_VALUE,
    REDACTED,
    UNSTRUCTURED_LINE_MAX_BYTES,
)

ROOT = Path(__file__).resolve().parents[2]
FB_DIR = ROOT / "deploy" / "logging" / "fluent-bit"
PROJECT = "avp2-luatest"
CONTAINER_ID = "c" * 64


def _fluent_bit_image() -> str:
    compose = yaml.safe_load((ROOT / "deploy/logging/compose.logging.yaml").read_text("utf-8"))
    return compose["services"]["fluent-bit"]["image"]


def _require_docker(image: str) -> None:
    if shutil.which("docker") is None:
        pytest.skip("docker が無い")
    probe = subprocess.run(
        ["docker", "image", "inspect", image], capture_output=True, text=True, check=False
    )
    if probe.returncode != 0:
        pytest.skip(f"{image} が手元に無い（docker pull してから実行）")


def _docker_line(log: str, *, stream: str = "stdout", app: bool = True, project: str = PROJECT):
    attrs = {
        "com.docker.compose.project": project,
        "com.docker.compose.service": "api",
        "tag": "avp2-luatest-api-1",
    }
    if app:
        attrs[APP_LOG_LABEL] = APP_LOG_LABEL_VALUE
    return json.dumps(
        {"log": log, "stream": stream, "attrs": attrs, "time": "2026-09-30T01:02:03.456789Z"}
    )


def _run(tmp_path: Path, lines: list[str]) -> list[dict[str, Any]]:
    image = _fluent_bit_image()
    _require_docker(image)
    cdir = tmp_path / "containers" / CONTAINER_ID
    cdir.mkdir(parents=True)
    (cdir / f"{CONTAINER_ID}-json.log").write_text("\n".join(lines) + "\n", encoding="utf-8")

    conf = yaml.safe_load((FB_DIR / "fluent-bit.yaml").read_text(encoding="utf-8"))
    conf.pop("includes", None)
    service = conf["service"]
    for key in [k for k in service if k.startswith("storage.") or k.startswith("hc_")]:
        service.pop(key)
    service.update({"http_server": "off", "health_check": "off", "flush": 1})
    tail = conf["pipeline"]["inputs"][0]
    tail.update({"read_from_head": "true", "db": "/tmp/tail.db", "exit_on_eof": "true"})
    tail.pop("storage.type", None)
    for flt in conf["pipeline"]["filters"]:
        flt.pop("emitter_storage.type", None)
    conf["pipeline"]["outputs"] = [{"name": "stdout", "match": "avp.*", "format": "json_lines"}]
    cfg = tmp_path / "test.yaml"
    cfg.write_text(yaml.safe_dump(conf, sort_keys=False), encoding="utf-8")

    proc = subprocess.run(
        [
            "docker", "run", "--rm", "--network", "none",
            "-e", f"AVP_LOG_TARGET_PROJECT={PROJECT}",
            "-e", "AVP_LOG_HOST_NAME=test-host",
            "-e", "AVP_LOG_READ_FROM_HEAD=true",
            "-v", f"{tmp_path / 'containers'}:/containers:ro",
            "-v", f"{FB_DIR}:/fluent-bit/etc/avp:ro",
            "-v", f"{cfg}:/test.yaml:ro",
            image, "-c", "/test.yaml",
        ],
        capture_output=True, text=True, timeout=120, check=False,
    )  # fmt: skip
    records = []
    for line in proc.stdout.splitlines():
        if line.startswith("{"):
            records.append(json.loads(line))
    assert proc.returncode == 0, proc.stderr[-2000:]
    return records


def _app(**fields: Any) -> str:
    base = {
        "@timestamp": "2026-09-30T00:00:00.123Z",
        "schema_version": 1,
        "event_id": "e-1",
        "event_name": "log.record",
        "level": "INFO",
        "message": "hello",
        "service_name": "api",
        "environment": "test",
        "git_sha": "unknown",
        "logger": "t",
    }
    base.update(fields)
    return json.dumps(base) + "\n"


def test_routing_repair_and_sanitize(tmp_path: Path) -> None:
    lines = [
        _docker_line(_app(event_id="ok-1")),
        # 型が合わない: boolean に "yes"、keyword に object、未知のキー、Collector のフィールド
        _docker_line(
            _app(event_id="bad-1", retryable="yes", scene_id={"x": 1}, extra=1, log_source="x")
        ),
        # @timestamp が不正 → Docker の時刻に置換
        _docker_line(_app(event_id="ts-1", **{"@timestamp": "not-a-date"})),
        # 秘密（整形器の取りこぼし）
        _docker_line(_app(event_id="sec-1", message="Authorization: Bearer abc.def.ghi")),
        # app の label でも JSON でない行・stderr は infra
        _docker_line("Traceback (most recent call last):\n", stream="stderr"),
        # label の無い（infra）コンテナ。postgres の DSN を含む長い行
        _docker_line(
            "FATAL: postgresql+psycopg://avp:pw@db/x " + "ab " * UNSTRUCTURED_LINE_MAX_BYTES,
            app=False,
        ),
        # 別の compose project は捨てる
        _docker_line(_app(event_id="other-1"), project=PROJECT + "-x"),
        # `{` で始まるが壊れた JSON
        _docker_line('{"event_id": "broken"\n'),
    ]
    got = _run(tmp_path, lines)
    by_id = {r.get("event_id"): r for r in got if r.get("event_id")}
    infra = [r for r in got if r.get("log_source") == "unstructured"]

    assert "other-1" not in by_id
    ok = by_id["ok-1"]
    assert ok["log_source"] == "app_json"
    assert ok["compose_service"] == "api" and ok["compose_project"] == PROJECT
    assert ok["container_name"] == "avp2-luatest-api-1" and ok["host_name"] == "test-host"
    assert ok["stream"] == "stdout"
    assert "@timestamp" not in ok, "時刻は record の timestamp に移し、出力側が1つだけ書く"
    assert abs(ok["date"] - 1790726400.123) < 0.001
    assert "collector_errors" not in ok
    assert not {"attrs", "time", "log", "_avp_route"} & set(ok)

    bad = by_id["bad-1"]
    assert sorted(bad["collector_errors"]) == ["extra", "log_source", "retryable", "scene_id"]
    assert bad["attributes"]["collector_moved"]["retryable"] == "yes"
    assert "retryable" not in bad and bad["log_source"] == "app_json"

    ts = by_id["ts-1"]
    assert ts["collector_errors"] == ["@timestamp_replaced"]
    assert abs(ts["date"] - 1790730123.456) < 0.01

    sec = by_id["sec-1"]
    assert "abc.def.ghi" not in json.dumps(sec) and REDACTED in sec["message"]
    assert sec["redaction_applied"] is True

    assert len(infra) == 3, infra
    traceback = next(r for r in infra if r["stream"] == "stderr")
    assert traceback["message"].startswith("Traceback")
    fatal = next(r for r in infra if r["message"].startswith("FATAL"))
    assert "avp:pw@" not in fatal["message"] and fatal["redaction_applied"] is True
    assert len(fatal["message"].encode()) <= UNSTRUCTURED_LINE_MAX_BYTES
    assert fatal["truncated"] is True
    broken = next(r for r in infra if "broken" in r["message"])
    assert broken["collector_errors"] == ["json_parse_failed"]
