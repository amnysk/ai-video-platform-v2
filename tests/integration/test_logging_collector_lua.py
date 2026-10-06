"""Collector（Fluent Bit + Lua）の振り分け・型修復・安全化を、採用版のイメージで実際に通す。

本物の ``deploy/logging/fluent-bit/fluent-bit.yaml`` の filter 列をそのまま使い、入力を
Docker json-file 形式の一時ファイル、出力を stdout に差し替える。OpenSearch は使わない。理由は
docs/testing/logging-platform-rationale.md §3。docker と Fluent Bit のイメージが無ければ skip。
"""

from __future__ import annotations

import json
import shutil
import subprocess
import time
from pathlib import Path
from typing import Any

import pytest
import yaml

from contracts.log_contract import (
    APP_LOG_LABEL,
    APP_LOG_LABEL_VALUE,
    COLLECTOR_LINE_MAX_BYTES,
    INFRA_FIELD_NAMES,
    LOG_FIELD_NAMES,
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


def _run(
    tmp_path: Path,
    lines: list[str],
    tail_overrides: dict[str, str] | None = None,
    *,
    by_tag: bool = False,
) -> list[dict[str, Any]]:
    """filter 列に通した record。``by_tag`` なら出力を tag ごとのファイルにし、各 record に
    ``_tag``（試験側で足す。Fluent Bit の出力には無い）を付けて返す（行き先の系統を確かめる）。"""
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
    tail.update(tail_overrides or {})
    tail.pop("storage.type", None)
    for flt in conf["pipeline"]["filters"]:
        flt.pop("emitter_storage.type", None)
    # 出力側（opensearch output）と同じく tv_nsec の切り捨てで時刻を文字列にする形式
    conf["pipeline"]["outputs"] = [
        {
            "name": "stdout",
            "match": "avp.*",
            "format": "json_lines",
            "json_date_format": "iso8601",
        }
    ]
    out_dir = tmp_path / "out"
    out_dir.mkdir()
    if by_tag:
        conf["pipeline"]["outputs"] = [
            {"name": "file", "match": "avp.*", "path": "/out", "format": "plain"}
        ]
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
            "-v", f"{out_dir}:/out",
            image, "-c", "/test.yaml",
        ],
        capture_output=True, text=True, timeout=120, check=False,
    )  # fmt: skip
    records = []
    for line in proc.stdout.splitlines():
        if line.startswith("{"):
            records.append(json.loads(line))
    assert proc.returncode == 0, proc.stderr[-2000:]
    if by_tag:
        for f in sorted(out_dir.iterdir()):
            for line in f.read_text(encoding="utf-8").splitlines():
                records.append({**json.loads(line), "_tag": f.name})
    return records


def _ms(record: dict[str, Any]) -> str:
    """iso8601 の date を、出力側が書くミリ秒精度の文字列に切り詰める。"""
    date = record["date"]
    return date[:23] + "Z"


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
        # _id にできない event_id（512 bytes 超）。そのまま送ると bulk の request 全体が 400 に
        # なり、
        # 同じ chunk の正常な行まで再送の末に破棄される（実測）
        _docker_line(_app(event_id="L" * 600, message="long-id")),
        # ミリ秒が double の誤差で1つ下に丸められないこと（.001 → .000 にしない）
        _docker_line(_app(event_id="ms-1", **{"@timestamp": "2026-09-30T03:40:00.001Z"})),
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
    assert _ms(ok) == "2026-09-30T00:00:00.123Z"
    assert "collector_errors" not in ok
    assert not {"attrs", "time", "log", "_avp_route"} & set(ok)

    bad = by_id["bad-1"]
    assert sorted(bad["collector_errors"]) == ["extra", "log_source", "retryable", "scene_id"]
    assert bad["attributes"]["collector_moved"]["retryable"] == "yes"
    assert "retryable" not in bad and bad["log_source"] == "app_json"

    ts = by_id["ts-1"]
    assert ts["collector_errors"] == ["@timestamp_replaced"]
    assert _ms(ts) == "2026-09-30T01:02:03.456Z"

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
    long_id = next(r for r in got if r.get("message") == "long-id")
    # 出力が直前の record の _id を使い回さないよう、app 系統は必ず event_id を持つ（実測）
    assert long_id["event_id"].startswith("collector-")
    assert long_id["collector_errors"] == ["event_id"]
    assert long_id["attributes"]["collector_moved"]["event_id"] == "L" * 600

    # 出力側は tv_nsec を切り捨てて .%03 を書く。double の誤差で .000 にならないこと
    assert _ms(by_id["ms-1"]) == "2026-09-30T03:40:00.001Z"

    broken = next(r for r in infra if "broken" in r["message"])
    assert broken["collector_errors"] == ["json_parse_failed"]
    # JSON として壊れた行は infra 系統（ADR-0040 §1・platform.md §1。I-19）。
    # infra は event_id を持たない
    assert "event_id" not in broken

    # Lua が書くキーは、行き先の mapping にあるものだけ（dynamic:false で黙って検索できなく
    # ならない。I-7）。
    # date は stdout 出力が付ける時刻（OpenSearch には @timestamp として出力側が書く）
    for r in got:
        keys = set(r) - {"date"}
        if r.get("log_source") == "unstructured" and "event_id" not in r:
            assert keys <= INFRA_FIELD_NAMES, keys - INFRA_FIELD_NAMES
        else:
            assert keys <= LOG_FIELD_NAMES, keys - LOG_FIELD_NAMES


# ------------------------------------------------------------------ 長い行（I-15）

#: Docker json-file はアプリの1行を 16384 bytes ごとの partial に分ける
#: （最後の partial だけが改行で終わる）
DOCKER_PARTIAL_BYTES = 16384


def _docker_partials(log: str, *, app: bool = True) -> list[str]:
    """1行を Docker と同じく partial に分けた json-file の行。

    tail の docker multiline parser が結合する。
    """
    lines = []
    while len(log) > DOCKER_PARTIAL_BYTES:
        lines.append(_docker_line(log[:DOCKER_PARTIAL_BYTES], app=app))
        log = log[DOCKER_PARTIAL_BYTES:]
    lines.append(_docker_line(log, app=app))
    return lines


def test_long_lines_do_not_stall_the_collector(tmp_path: Path) -> None:
    """partial を結合した長い行で filter 列が止まらず、後続の行が読まれること（I-15）。

    結合後の行には tail の ``buffer_max_size`` が効かない
    （16KiB の partial ごとに判定される。実測）。
    修正前は安全化の Lua パターンが長い英数字の連なりで O(n²) になり、300k の1行で Fluent Bit の
    イベントループ（全ファイルの tail を含む）が CPU 100% のまま止まった（隔離環境で実測）。
    """
    pathological = {
        "run": "x" * 200_000,  # 英数字だけの連なり（userinfo の scheme 規則）
        "hex": "0123456789abcdef" * 12_000,
        "jwt": "eyJ" * 60_000,
        "url": "http://h" * 20_000,
        "params": "[parameters: " * 12_000,
        "pem": "-----BEGIN PRIVATE KEY-----" * 6_000,
    }
    lines: list[str] = []
    for name, body in pathological.items():
        lines += _docker_partials(_app(event_id=f"big-{name}", message=body))
        lines += _docker_partials(body + "\n", app=False)
    # 上限（COLLECTOR_LINE_MAX_BYTES）を超える行: infra へ切り詰めて送り、line_too_long を残す
    lines += _docker_partials(_app(event_id="huge-1", message="y" * (COLLECTOR_LINE_MAX_BYTES + 1)))
    lines += [_docker_line(_app(event_id=f"after-{i}")) for i in range(50)]

    started = time.monotonic()
    # exit_on_eof は buffer_chunk_size より長い行の途中で EOF と判定して止まる（5.1.2、実測）。
    # 試験の終了判定のためだけに、ファイル全体を1回で読める大きさにする（停止の再現は filter 側）
    got = _run(tmp_path, lines, {"buffer_chunk_size": "8M", "buffer_max_size": "8M"})
    elapsed = time.monotonic() - started

    by_id = {r.get("event_id"): r for r in got if r.get("event_id")}
    assert {f"after-{i}" for i in range(50)} <= set(by_id), "長い行の後の行が読まれない"
    for name in pathological:
        assert f"big-{name}" in by_id, name
    infra = [r for r in got if r.get("log_source") == "unstructured"]
    too_long = [r for r in infra if "line_too_long" in (r.get("collector_errors") or [])]
    assert len(too_long) == 1, [r.get("collector_errors") for r in infra]
    assert too_long[0]["truncated"] is True
    assert len(too_long[0]["message"].encode()) <= UNSTRUCTURED_LINE_MAX_BYTES
    # 上限を超えた行は app の文書にしない（切り詰めた JSON は壊れている）
    assert "huge-1" not in by_id
    # 修正前は 1行で数分〜止まったまま。線形なら全体で数秒（起動込み）
    assert elapsed < 60, f"{elapsed:.1f}s"


def test_sanitize_rules_keep_redacting_after_the_linear_rewrite(tmp_path: Path) -> None:
    """線形化した規則（userinfo・query・SQL parameters・PEM・JWT）が同じ値を伏せること（I-15）。"""
    cases = {
        "dsn": ("connect postgresql+psycopg://avp:s3cr3t@db:5432/x failed", "s3cr3t"),
        "dsn2": ("a://b x redis://:pw9@cache/0 y", "pw9"),
        "query": ("GET https://bucket.example/obj?X-Amz-Signature=abc123 200", "abc123"),
        "fragment": ("see https://h.example/p#tok=frag42 now", "frag42"),
        "params": ("INSERT ... [parameters: ('hunter2', 1)] done", "hunter2"),
        "params_open": ("INSERT ... [parameters: ('hunter3', 1) (truncated", "hunter3"),
        "pem": ("-----BEGIN PRIVATE KEY-----\nMIIEvQ\n-----END PRIVATE KEY----- ok", "MIIEvQ"),
        "pem_open": ("-----BEGIN EC PRIVATE KEY-----\nMHcCAQ (cut", "MHcCAQ"),
        "jwt": ("token eyJhbGc.eyJzdWIi.c2lnbmF0 end", "c2lnbmF0"),
    }
    lines = [_docker_line(_app(event_id=k, message=m)) for k, (m, _) in cases.items()]
    got = _run(tmp_path, lines)
    by_id = {r.get("event_id"): r for r in got if r.get("event_id")}
    for key, (_, secret) in cases.items():
        assert secret not in json.dumps(by_id[key]), key
        assert by_id[key]["redaction_applied"] is True, key
    assert by_id["dsn"]["message"].startswith("connect postgresql+psycopg://"), "scheme は残す"
    assert "https://bucket.example/obj 200" in by_id["query"]["message"]
    assert by_id["pem"]["message"].endswith(" ok")


# ---------------------------------------------------------- 非有限値・壊れた JSON（I-18, I-19）


def test_non_finite_numbers_are_moved_aside(tmp_path: Path) -> None:
    """数値フィールドの "inf"/"nan" を数値として送らない（I-18）。

    Lua の tonumber は "inf"・"nan"・"1e400" を非有限の数にする。OpenSearch はそれを含む bulk を
    chunk ごと拒否し、同じ chunk の正常な行まで届かなかった（担当C の隔離試験で 41 行中 0 件、
    再送は metrics の破棄・エラーに出ない）。既存の型不整合と同じく退避する。
    """
    bad = {"inf": "inf", "ninf": "-inf", "nan": "nan", "big": "1e400", "hexinf": "-INF"}
    lines = [_docker_line(_app(event_id=f"nf-{k}", duration_ms=v)) for k, v in bad.items()]
    lines.append(_docker_line(_app(event_id="nf-ok", duration_ms="12", scene_revision=3)))
    got = _run(tmp_path, lines, by_tag=True)
    by_id = {r.get("event_id"): r for r in got if r.get("event_id")}
    for k, v in bad.items():
        r = by_id[f"nf-{k}"]
        assert "duration_ms" not in r, k
        assert "duration_ms" in r["collector_errors"], k
        assert r["attributes"]["collector_moved"]["duration_ms"] == v, k
        assert r["_tag"] == "avp.app"
    ok = by_id["nf-ok"]
    assert ok["duration_ms"] == 12 and ok["scene_revision"] == 3
    assert "collector_errors" not in ok


def test_broken_json_goes_to_the_infra_series(tmp_path: Path) -> None:
    """``{`` で始まるが JSON として壊れた行は infra 系統へ（I-19）。

    修正前は app の index に契約の必須フィールド無しで入った（担当C の隔離試験）。
    """
    lines = [
        _docker_line('{"event_id": "broken-1", "message": "cut\n'),
        _docker_line('{"duration_ms": 1e400}\n'),  # JSON の数値として溢れる（解釈できない）
        _docker_line(_app(event_id="fine-1")),
    ]
    got = _run(tmp_path, lines, by_tag=True)
    infra = [r for r in got if r["_tag"] == "avp.infra"]
    app = [r for r in got if r["_tag"] == "avp.app"]
    assert [r["event_id"] for r in app] == ["fine-1"]
    assert len(infra) == 2, infra
    for r in infra:
        assert r["log_source"] == "unstructured"
        assert r["collector_errors"] == ["json_parse_failed"]
        assert "event_id" not in r
        assert set(r) - {"_tag"} <= INFRA_FIELD_NAMES, set(r) - INFRA_FIELD_NAMES
    assert any("broken-1" in r["message"] for r in infra)


def test_linear_rules_do_not_leak_what_the_old_rules_redacted(tmp_path: Path) -> None:
    """線形化（I-15）で生じた伏せ漏れ（D の再確認 I-22・I-23）。

    修正前の規則（87fd105）はどちらも伏せていた。
    """
    cases = {
        # JWT の直前に形の合わない `eyJ.` がある（I-22）
        "jwt_after_eyj": ("a eyJ.eyJhbGc.eyJzdWIi.c2lnbmF0 b", ["eyJzdWIi", "c2lnbmF0"]),
        "jwt_after_two": ("eyJ.eyJ.eyJhbGc.eyJzdWIi.c2lnbmF1", ["eyJzdWIi", "c2lnbmF1"]),
        "jwt_mid_segment": ("xeyJ.aeyJb.eyJzdWIi.c2lnbmF2", ["eyJzdWIi", "c2lnbmF2"]),
        # 閉じていない BEGIN の後に別の鍵ブロック（I-23）
        "pem_two_begins": (
            "-----BEGIN PRIVATE KEY-----\nKEYONE\n-----BEGIN EC PRIVATE KEY-----\n"
            "KEYTWO\n-----END EC PRIVATE KEY----- tail",
            ["KEYONE", "KEYTWO"],
        ),
    }
    lines = [_docker_line(_app(event_id=k, message=m)) for k, (m, _) in cases.items()]
    got = _run(tmp_path, lines)
    by_id = {r.get("event_id"): r for r in got if r.get("event_id")}
    for key, (_, secrets) in cases.items():
        for secret in secrets:
            assert secret not in json.dumps(by_id[key]), (key, secret, by_id[key]["message"])
    assert by_id["pem_two_begins"]["message"].endswith(" tail")
