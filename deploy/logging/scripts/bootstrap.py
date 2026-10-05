#!/usr/bin/env python3
"""OpenSearch の index 基盤を冪等に作る（ADR-0040 §4）。

one-shot（OpenSearch のイメージの Python 3.9）で動く。

順序: ingest pipeline → component template（生成物）→ 環境ごとの index template（rollover_alias）
→ ISM policy（ism_template は対象 prefix だけ）→ 初期 index と write alias → 検証。

- 何度流しても同じ状態になる。既存の policy は内容が違うときだけ更新する
  （管理中の index は旧版のまま。ISM の仕様）。
- write alias の名前が実 index になっていたら（alias 誤作成）、何も直さず失敗する。
- 全 index を対象にする操作はしない（対象は avp-app-<env>-* / avp-infra-<env>-* だけ）。

認証は admin 証明書（admin_dn）。環境変数:
  AVP_LOGGING_ENV       環境名（prod / test …）
  OPENSEARCH_URL        既定 https://opensearch:9200
  AVP_ADMIN_CERT/KEY/CA 既定 /secrets/pki/{admin.pem,admin.key,ca.pem}
  AVP_DEPLOY_DIR        既定 /deploy（deploy/logging/opensearch を ro で mount）
"""

from __future__ import annotations

import json
import os
import re
import ssl
import sys
import time
import urllib.error
import urllib.request
from typing import Any

ENV = os.environ.get("AVP_LOGGING_ENV", "")
URL = os.environ.get("OPENSEARCH_URL", "https://opensearch:9200").rstrip("/")
CERT = os.environ.get("AVP_ADMIN_CERT", "/secrets/pki/admin.pem")
KEY = os.environ.get("AVP_ADMIN_KEY", "/secrets/pki/admin.key")
CA = os.environ.get("AVP_ADMIN_CA", "/secrets/pki/ca.pem")
DEPLOY = os.environ.get("AVP_DEPLOY_DIR", "/deploy")
#: 試験用に短縮した policy を使うとき ``ism-test``（本番は ``ism``）
ISM_DIR = os.environ.get("AVP_ISM_POLICY_DIR", "ism") or "ism"
#: 試験用に ISM の実行間隔（分）を縮めるとき。空なら触らない（既定 5 分）
ISM_JOB_INTERVAL = os.environ.get("AVP_ISM_JOB_INTERVAL_MIN", "")

EXPECTED_AUTO_CREATE = "-avp-*,+*"
PIPELINE = "avp-ingest"
#: series -> (component template file, ISM policy file)
SERIES = {
    "app": ("templates/avp-app-mappings.json", "avp-app.json"),
    "infra": ("templates/avp-infra-mappings.json", "avp-infra.json"),
}


class BootstrapError(Exception):
    pass


def _ctx() -> ssl.SSLContext:
    ctx = ssl.create_default_context(cafile=CA)
    ctx.load_cert_chain(CERT, KEY)
    return ctx


CTX: ssl.SSLContext | None = None


def call(method: str, path: str, body: Any = None, ok=(200, 201)) -> tuple[int, Any]:
    global CTX
    if CTX is None:
        CTX = _ctx()
    data = None if body is None else json.dumps(body).encode()
    req = urllib.request.Request(URL + path, data=data, method=method)
    req.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(req, context=CTX, timeout=30) as resp:
            status, raw = resp.status, resp.read()
    except urllib.error.HTTPError as e:
        status, raw = e.code, e.read()
    parsed = json.loads(raw) if raw else None
    if ok is not None and status not in ok:
        raise BootstrapError(f"{method} {path} -> {status}: {raw[:500]!r}")
    return status, parsed


def load(rel: str) -> Any:
    with open(os.path.join(DEPLOY, rel), encoding="utf-8") as f:
        return json.load(f)


def wait_ready(timeout_s: int = 300) -> None:
    deadline = time.time() + timeout_s
    while True:
        try:
            status, body = call(
                "GET", "/_cluster/health?wait_for_status=yellow&timeout=10s", ok=None
            )
            if status == 200 and body and body.get("status") in ("yellow", "green"):
                return
        except (urllib.error.URLError, OSError, ValueError):
            pass
        if time.time() > deadline:
            raise BootstrapError("OpenSearch が準備できない")
        time.sleep(3)


def check_auto_create() -> None:
    _, body = call("GET", "/_cluster/settings?include_defaults=true&flat_settings=true")
    value = None
    for section in ("transient", "persistent", "defaults"):
        value = value or body.get(section, {}).get("action.auto_create_index")
    if value != EXPECTED_AUTO_CREATE:
        raise BootstrapError(f"action.auto_create_index={value!r}（期待 {EXPECTED_AUTO_CREATE!r}）")
    print(f"ok   action.auto_create_index={value}")


def set_ism_job_interval() -> None:
    if not ISM_JOB_INTERVAL:
        return
    if not ISM_JOB_INTERVAL.isdigit():
        raise BootstrapError(f"AVP_ISM_JOB_INTERVAL_MIN が不正: {ISM_JOB_INTERVAL!r}")
    body = {"persistent": {"plugins.index_state_management.job_interval": int(ISM_JOB_INTERVAL)}}
    call("PUT", "/_cluster/settings", body)
    print(f"ok   ISM job_interval={ISM_JOB_INTERVAL}m（試験用）")


def put_pipeline() -> None:
    call("PUT", f"/_ingest/pipeline/{PIPELINE}", load("ingest/avp-ingest.json"))
    print(f"ok   ingest pipeline {PIPELINE}")


def put_component(series: str) -> str:
    name = f"avp-{series}-mappings"
    call("PUT", f"/_component_template/{name}", load(SERIES[series][0]))
    print(f"ok   component template {name}")
    return name


def put_index_template(series: str, component: str) -> None:
    prefix = f"avp-{series}-{ENV}"
    body = {
        "index_patterns": [f"{prefix}-*"],
        "priority": 200,
        "composed_of": [component],
        "template": {
            "settings": {
                "index.number_of_shards": 1,
                "index.number_of_replicas": 0,
                "index.default_pipeline": PIPELINE,
                # rollover で作られる次の index も template からこの値を受け取る
                "plugins.index_state_management.rollover_alias": f"{prefix}-write",
            }
        },
        "_meta": {"managed_by": "deploy/logging/scripts/bootstrap.py", "env": ENV},
    }
    call("PUT", f"/_index_template/{prefix}", body)
    print(f"ok   index template {prefix}")


def _normalize_policy(policy: dict) -> dict:
    """比較用。サーバーが足す retry・時刻・版を落とす。"""
    p = json.loads(json.dumps(policy))
    for key in ("policy_id", "last_updated_time", "schema_version", "error_notification"):
        p.pop(key, None)
    for state in p.get("states", []):
        for action in state.get("actions", []):
            action.pop("retry", None)
            for v in action.values():
                if isinstance(v, dict):
                    v.pop("copy_alias", None)
    for t in p.get("ism_template") or []:
        t.pop("last_updated_time", None)
    return p


def put_policy(series: str) -> None:
    policy_id = f"avp-{series}"
    desired = load(os.path.join(ISM_DIR, SERIES[series][1]))
    status, current = call("GET", f"/_plugins/_ism/policies/{policy_id}", ok=(200, 404))
    if status == 404:
        call("PUT", f"/_plugins/_ism/policies/{policy_id}", desired)
        print(f"ok   ISM policy {policy_id} created")
        return
    if _normalize_policy(current["policy"]) == _normalize_policy(desired["policy"]):
        print(f"ok   ISM policy {policy_id} unchanged")
        return
    q = f"?if_seq_no={current['_seq_no']}&if_primary_term={current['_primary_term']}"
    call("PUT", f"/_plugins/_ism/policies/{policy_id}{q}", desired)
    print(f"ok   ISM policy {policy_id} updated（既存の管理対象 index は旧版のまま）")


def check_alias_not_an_index(series: str) -> None:
    """alias 名が実 index になっていないか（auto_create と権限の二重防御をすり抜けた場合の検出）。

    何かを書く前に確かめる（誤作成を見つけたら何も変えずに止まる）。
    """
    alias = f"avp-{series}-{ENV}-write"
    status, body = call("GET", f"/{alias}?filter_path=*.aliases", ok=(200, 404))
    if status == 200 and alias in (body or {}):
        raise BootstrapError(f"{alias} が実 index として存在する（alias 誤作成）。手で調べること")


def ensure_write_index(series: str) -> None:
    prefix = f"avp-{series}-{ENV}"
    alias = f"{prefix}-write"
    first = f"{prefix}-000001"
    check_alias_not_an_index(series)
    status, body = call("GET", f"/_alias/{alias}", ok=(200, 404))
    if status == 200:
        writers = [i for i, v in body.items() if v["aliases"][alias].get("is_write_index")]
        if len(writers) != 1:
            raise BootstrapError(f"{alias} の write index が1つでない: {sorted(body)}")
        print(f"ok   write alias {alias} -> {writers[0]}")
        attach_policy(writers[0], f"avp-{series}")
        return
    status, _ = call("HEAD", f"/{first}", ok=(200, 404))
    if status == 200:
        raise BootstrapError(f"{first} はあるが {alias} が無い。手で調べること")
    call("PUT", f"/{first}", {"aliases": {alias: {"is_write_index": True}}})
    print(f"ok   created {first} with write alias {alias}")
    attach_policy(first, f"avp-{series}")


def attach_policy(index: str, policy_id: str) -> None:
    """ism_template は policy 作成後の index にしか付かない。付いていなければ付ける。"""
    for _ in range(10):
        _, body = call("GET", f"/_plugins/_ism/explain/{index}")
        info = (body or {}).get(index) or {}
        current = info.get("index.plugins.index_state_management.policy_id") or info.get(
            "policy_id"
        )
        if current == policy_id:
            print(f"ok   {index} managed by {policy_id}")
            return
        if current:
            raise BootstrapError(f"{index} は別の policy {current} で管理されている")
        time.sleep(2)
    call("POST", f"/_plugins/_ism/add/{index}", {"policy_id": policy_id})
    print(f"ok   attached {policy_id} to {index}")


def main() -> int:
    if not re.fullmatch(r"[a-z][a-z0-9]{0,15}", ENV):
        print(f"AVP_LOGGING_ENV が不正: {ENV!r}", file=sys.stderr)
        return 2
    try:
        wait_ready()
        check_auto_create()
        for s in SERIES:
            check_alias_not_an_index(s)
        set_ism_job_interval()
        put_pipeline()
        components = {s: put_component(s) for s in SERIES}
        for s in SERIES:
            put_index_template(s, components[s])
        for s in SERIES:
            put_policy(s)
        for s in SERIES:
            ensure_write_index(s)
    except BootstrapError as e:
        print(f"FAIL {e}", file=sys.stderr)
        return 1
    print("bootstrap: done")
    return 0


if __name__ == "__main__":
    sys.exit(main())
