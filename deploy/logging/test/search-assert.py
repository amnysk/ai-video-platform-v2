"""OpenSearch に TLS + 認証で問い合わせ、件数・フィールド・運用状態を assert する（検証用）。

標準ライブラリだけで動く（ホストの python でも、repo の .venv でも）。失敗は非0で終了し、
結果は1行 JSON で stdout に出す（資格情報・レスポンス全文は出さない）。

接続先と資格情報は env とファイルから読む（引数やコマンド履歴にパスワードを残さない）:

- ``OPENSEARCH_URL``            既定 ``https://127.0.0.1:9200``
- ``OPENSEARCH_CA``             CA 証明書のパス（必須。検証を切るオプションは持たない）
- ``OPENSEARCH_USER``           既定 ``avp_viewer``（検索は読み取りユーザーで行う）
- ``OPENSEARCH_PASSWORD_FILE``  パスワードを1行で書いたファイル（0600 を推奨）
- ``OPENSEARCH_INDEX``          検索対象（既定 ``avp-app-test-*``）
- ``OPENSEARCH_CLIENT_CERT`` / ``OPENSEARCH_CLIENT_KEY``  指定すると Basic ではなく証明書で認証する
  （``alias`` / ``ism`` / ``snapshot`` は viewer に権限が無いので admin 証明書で読む。読み取りだけ）

サブコマンド（例）::

    search-assert.py count --term request_id=rot-1 --expect 5000
    search-assert.py count --term episode_id=ep1 --term event_name=scene.rejected --min 1
    search-assert.py fields --term episode_id=ep1 --require-contract   # REQUIRED_APP_FIELDS
    search-assert.py fields --term episode_id=ep1 --require provider_request_id,scene_id
    search-assert.py distinct --term episode_id=ep1 --field episode_id --expect-values ep1
    search-assert.py dupes --term request_id=rot-1 --expect 0          # event_id の重複文書
    search-assert.py absent --needles-file needles.txt   # _source 全走査で文字列が無い
    search-assert.py collector-errors --term request_id=bad-1 --min 1
    search-assert.py alias --alias avp-app-test-write --expect-write-index avp-app-test-000002
    search-assert.py ism --index 'avp-app-test-*' --policy avp-app-test-policy
    search-assert.py version --expect 3.8.0
    search-assert.py snapshot --out before.json                         # bootstrap 再実行の差分用
    search-assert.py lag --max-seconds 120                              # 最新 ingested_at の遅れ
    search-assert.py agg --term container_name=x --by episode_id,event_name --table
"""

from __future__ import annotations

import argparse
import base64
import contextlib
import json
import os
import ssl
import sys
import time
import urllib.error
import urllib.request
from datetime import UTC, datetime
from pathlib import Path
from typing import Any


class AssertFailed(Exception):
    pass


# --------------------------------------------------------------------------- 接続


def _client_config() -> tuple[str, ssl.SSLContext, str]:
    url = os.environ.get("OPENSEARCH_URL", "https://127.0.0.1:9200").rstrip("/")
    ca = os.environ.get("OPENSEARCH_CA")
    if not ca:
        raise SystemExit("OPENSEARCH_CA が未設定（TLS 検証は切らない）")
    ctx = ssl.create_default_context(cafile=ca)
    ctx.check_hostname = os.environ.get("OPENSEARCH_VERIFY_HOSTNAME", "1") != "0"
    # Python 3.13 は既定で VERIFY_X509_STRICT（CA に keyUsage が無いと拒否）。B の CA は keyUsage を
    # 持たない（verification-results の F-B1）。鎖と hostname の検証は保ったまま strict だけ外す
    if os.environ.get("OPENSEARCH_X509_STRICT", "0") != "1":
        ctx.verify_flags &= ~ssl.VERIFY_X509_STRICT
    cert = os.environ.get("OPENSEARCH_CLIENT_CERT")
    if cert:
        # admin 証明書（alias・ISM の explain・template の読み取りは viewer に権限が無い）
        ctx.load_cert_chain(cert, os.environ.get("OPENSEARCH_CLIENT_KEY"))
        return url, ctx, ""
    user = os.environ.get("OPENSEARCH_USER", "avp_viewer")
    pw_file = os.environ.get("OPENSEARCH_PASSWORD_FILE")
    if not pw_file:
        raise SystemExit("OPENSEARCH_PASSWORD_FILE が未設定")
    password = Path(pw_file).read_text(encoding="utf-8").strip()
    token = base64.b64encode(f"{user}:{password}".encode()).decode()
    return url, ctx, f"Basic {token}"


def request(method: str, path: str, body: Any | None = None) -> Any:
    url, ctx, auth = _client_config()
    data = None if body is None else json.dumps(body).encode()
    req = urllib.request.Request(url + path, data=data, method=method)
    if auth:
        req.add_header("Authorization", auth)
    if data is not None:
        req.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(req, context=ctx, timeout=30) as resp:
            return json.loads(resp.read() or b"null")
    except urllib.error.HTTPError as exc:
        # 応答本文は error.type / reason だけ出す（値のプレビューを出さない）
        try:
            err = json.loads(exc.read()).get("error", {})
            detail = {"type": err.get("type"), "reason": str(err.get("reason", ""))[:200]}
        except Exception:
            detail = {}
        raise AssertFailed(f"HTTP {exc.code} {method} {path} {detail}") from None
    except urllib.error.URLError as exc:
        raise AssertFailed(f"{method} {path}: {exc.reason}") from None


def _index() -> str:
    return os.environ.get("OPENSEARCH_INDEX", "avp-app-test-*")


# --------------------------------------------------------------------------- 検索条件


def build_query(terms: list[str], since: str | None) -> dict:
    filters: list[dict] = []
    for t in terms:
        field, _, value = t.partition("=")
        if not _:
            raise SystemExit(f"--term は field=value: {t!r}")
        filters.append({"term": {field: value}})
    if since:
        filters.append({"range": {"@timestamp": {"gte": since}}})
    return {"bool": {"filter": filters}}


def _refresh() -> None:
    # 検索前に refresh（viewer に権限が無ければ無視。refresh_interval 待ちの偽陰性を減らす）
    with contextlib.suppress(AssertFailed):
        request("POST", f"/{_index()}/_refresh")


def count(query: dict) -> int:
    return int(request("POST", f"/{_index()}/_count", {"query": query})["count"])


def iter_hits(query: dict, source: list[str] | bool = True, page: int = 1000):
    """search_after で全件を回す（PIT を使わない簡易版。試験中の新規書き込みは拾い漏れ得る）。"""
    after = None
    while True:
        body: dict[str, Any] = {
            "size": page,
            "query": query,
            "sort": [{"@timestamp": "asc"}, {"event_id": "asc"}],
            "_source": source,
        }
        if after:
            body["search_after"] = after
        hits = request("POST", f"/{_index()}/_search", body)["hits"]["hits"]
        if not hits:
            return
        yield from hits
        after = hits[-1]["sort"]


def _check_bounds(actual: int, args: argparse.Namespace) -> None:
    if args.expect is not None and actual != args.expect:
        raise AssertFailed(f"expected {args.expect}, got {actual}")
    if args.min is not None and actual < args.min:
        raise AssertFailed(f"expected >= {args.min}, got {actual}")
    if args.max is not None and actual > args.max:
        raise AssertFailed(f"expected <= {args.max}, got {actual}")


# --------------------------------------------------------------------------- サブコマンド


def cmd_count(args) -> dict:
    q = build_query(args.term, args.since)
    deadline = time.monotonic() + args.wait
    while True:
        try:
            # 再起動直後の OpenSearch（TLS の EOF・接続拒否）も --wait の間は待つ
            _refresh()
            n = count(q)
            _check_bounds(n, args)
            return {"count": n}
        except AssertFailed:
            if time.monotonic() >= deadline:
                raise
            time.sleep(2)


def _required_contract_fields() -> list[str]:
    sys.path.insert(0, str(Path(__file__).resolve().parents[3]))
    from contracts.log_contract import REQUIRED_APP_FIELDS

    return list(REQUIRED_APP_FIELDS)


def cmd_fields(args) -> dict:
    required = [f for f in (args.require or "").split(",") if f]
    if args.require_contract:
        required += _required_contract_fields()
    q = build_query(args.term, args.since)
    missing: dict[str, int] = {}
    n = 0
    for hit in iter_hits(q):
        n += 1
        src = hit["_source"]
        for f in required:
            if f not in src or src[f] in (None, ""):
                missing[f] = missing.get(f, 0) + 1
    if n == 0:
        raise AssertFailed("no documents matched")
    if missing:
        raise AssertFailed(f"missing fields in {n} docs: {missing}")
    return {"docs": n, "required": sorted(set(required))}


def cmd_distinct(args) -> dict:
    q = build_query(args.term, args.since)
    body = {
        "size": 0,
        "query": q,
        "aggs": {"v": {"terms": {"field": args.field, "size": 1000, "missing": "__missing__"}}},
    }
    buckets = request("POST", f"/{_index()}/_search", body)["aggregations"]["v"]["buckets"]
    values = {b["key"]: b["doc_count"] for b in buckets}
    if args.expect_values is not None:
        expected = set(args.expect_values.split(","))
        if set(values) != expected:
            raise AssertFailed(f"{args.field} values {sorted(values)} != {sorted(expected)}")
    return {"field": args.field, "values": values}


def cmd_dupes(args) -> dict:
    q = build_query(args.term, args.since)
    body = {
        "size": 0,
        "query": q,
        "aggs": {"d": {"terms": {"field": "event_id", "min_doc_count": 2, "size": 100}}},
    }
    buckets = request("POST", f"/{_index()}/_search", body)["aggregations"]["d"]["buckets"]
    _check_bounds(len(buckets), args)
    return {"duplicated_event_ids": len(buckets), "sample": [b["key"] for b in buckets[:5]]}


def cmd_absent(args) -> dict:
    needles = [
        line.strip()
        for line in Path(args.needles_file).read_text(encoding="utf-8").splitlines()
        if line.strip() and not line.startswith("#")
    ]
    if not needles:
        raise SystemExit("needles が空")
    q = build_query(args.term, args.since)
    n = 0
    found: dict[int, int] = {}
    for hit in iter_hits(q):
        n += 1
        raw = json.dumps(hit["_source"], ensure_ascii=False)
        for i, needle in enumerate(needles):
            if needle in raw:
                found[i] = found.get(i, 0) + 1
    if args.min_docs and n < args.min_docs:
        # 走査対象が空では「無い」ことの証明にならない
        raise AssertFailed(f"scanned only {n} docs (< {args.min_docs})")
    if found:
        # 値そのものは出さない（needles ファイルの行番号だけ）
        raise AssertFailed(f"needles found (line index -> docs): {found}")
    return {"scanned_docs": n, "needles": len(needles)}


def cmd_collector_errors(args) -> dict:
    q = build_query(args.term, args.since)
    q["bool"]["filter"].append({"exists": {"field": "collector_errors"}})
    body = {
        "size": 0,
        "query": q,
        "aggs": {"e": {"terms": {"field": "collector_errors", "size": 100}}},
    }
    res = request("POST", f"/{_index()}/_search", body)
    total = res["hits"]["total"]["value"]
    _check_bounds(total, args)
    return {
        "docs_with_collector_errors": total,
        "errors": {b["key"]: b["doc_count"] for b in res["aggregations"]["e"]["buckets"]},
    }


def cmd_alias(args) -> dict:
    res = request("GET", f"/_alias/{args.alias}")
    writes = [idx for idx, v in res.items() if v["aliases"][args.alias].get("is_write_index")]
    if args.expect_write_index and writes != [args.expect_write_index]:
        raise AssertFailed(f"write index {writes} != {args.expect_write_index}")
    if len(writes) != 1:
        raise AssertFailed(f"write index は1つであること: {writes}")
    # alias 名が実 index になっていないか（auto_create 事故）
    if args.alias in res:
        raise AssertFailed(f"{args.alias} が実 index として存在する")
    return {"alias": args.alias, "indices": sorted(res), "write_index": writes[0]}


def cmd_ism(args) -> dict:
    res = request("GET", f"/_plugins/_ism/explain/{args.index}")
    out = {}
    bad = []
    for idx, v in res.items():
        if not isinstance(v, dict) or idx == "total_managed_indices":
            continue
        policy = v.get("index.plugins.index_state_management.policy_id") or v.get("policy_id")
        state = (v.get("state") or {}).get("name")
        failed = (v.get("info") or {}).get("message") if v.get("failed") else None
        out[idx] = {"policy": policy, "state": state, "failed": failed}
        if args.policy and policy != args.policy:
            bad.append(f"{idx}: policy={policy}")
        if failed:
            bad.append(f"{idx}: failed={failed}")
    if args.expect_state and not any(v["state"] == args.expect_state for v in out.values()):
        bad.append(f"no index in state {args.expect_state}")
    if bad:
        raise AssertFailed("; ".join(bad))
    return {"indices": out}


def cmd_version(args) -> dict:
    info = request("GET", "/")
    number = info["version"]["number"]
    if args.expect and number != args.expect:
        raise AssertFailed(f"version {number} != {args.expect}")
    return {"version": number, "distribution": info["version"].get("distribution")}


def cmd_snapshot(args) -> dict:
    """bootstrap 再実行の前後で比べる設定（index template・ISM・pipeline・alias）。"""
    prefix = args.prefix

    def safe(path: str) -> Any:
        try:
            return request("GET", path)
        except AssertFailed as exc:
            return {"error": str(exc)}

    snap = {
        "index_templates": safe(f"/_index_template/{prefix}*"),
        "component_templates": safe(f"/_component_template/{prefix}*"),
        "ism_policies": safe("/_plugins/_ism/policies"),
        "ingest_pipelines": safe(f"/_ingest/pipeline/{prefix}*"),
        "aliases": safe(f"/_alias/{prefix}*"),
        "indices": sorted(i["index"] for i in safe(f"/_cat/indices/{prefix}*?format=json") or []),
    }
    # 実行ごとに変わる値（version・時刻）は比較から外す
    for p in (snap["ism_policies"] or {}).get("policies", []):
        for k in ("_seq_no", "_primary_term", "_version"):
            p.pop(k, None)
        p.get("policy", {}).pop("last_updated_time", None)
    Path(args.out).write_text(json.dumps(snap, sort_keys=True, indent=1), encoding="utf-8")
    return {"out": args.out, "indices": snap["indices"]}


def cmd_agg(args) -> dict:
    """``--by a,b,c`` の組ごとの件数（composite aggregation。欠けた値は ``-``）。調査・照合用。"""
    fields = args.by.split(",")
    q = build_query(args.term, args.since)
    sources = [{f: {"terms": {"field": f, "missing_bucket": True}}} for f in fields]
    rows: list[dict] = []
    after = None
    while True:
        comp: dict[str, Any] = {"size": 1000, "sources": sources}
        if after:
            comp["after"] = after
        body = {"size": 0, "query": q, "aggs": {"c": {"composite": comp}}}
        res = request("POST", f"/{_index()}/_search", body)["aggregations"]["c"]
        for b in res["buckets"]:
            key = {f: ("-" if b["key"][f] is None else b["key"][f]) for f in fields}
            rows.append({**key, "n": b["doc_count"]})
        after = res.get("after_key")
        if not res["buckets"] or not after:
            break
    if args.table:
        for r in rows:
            print("\t".join(str(r[f]) for f in [*fields, "n"]))
    return {"rows": len(rows)} if args.table else {"rows": rows}


def cmd_lag(args) -> dict:
    body = {"size": 0, "aggs": {"m": {"max": {"field": "ingested_at"}}}}
    res = request("POST", f"/{_index()}/_search", body)
    value = res["aggregations"]["m"].get("value_as_string")
    if not value:
        raise AssertFailed("ingested_at が1件も無い")
    last = datetime.fromisoformat(value.replace("Z", "+00:00"))
    lag = (datetime.now(UTC) - last).total_seconds()
    if args.max_seconds is not None and lag > args.max_seconds:
        raise AssertFailed(f"last ingested_at {value} is {lag:.0f}s old")
    return {"last_ingested_at": value, "lag_seconds": round(lag, 1)}


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawTextHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)

    def with_query(p):
        p.add_argument("--term", action="append", default=[], help="field=value（複数可・AND）")
        p.add_argument("--since", help="@timestamp の下限（ISO 8601）")
        return p

    def with_bounds(p):
        p.add_argument("--expect", type=int)
        p.add_argument("--min", type=int)
        p.add_argument("--max", type=int)
        return p

    p = with_bounds(with_query(sub.add_parser("count")))
    p.add_argument("--wait", type=float, default=0, help="条件を満たすまで待つ秒数")
    p.set_defaults(fn=cmd_count)

    p = with_query(sub.add_parser("fields"))
    p.add_argument("--require", help="カンマ区切りの必須フィールド")
    p.add_argument("--require-contract", action="store_true", help="REQUIRED_APP_FIELDS を足す")
    p.set_defaults(fn=cmd_fields)

    p = with_query(sub.add_parser("distinct"))
    p.add_argument("--field", required=True)
    p.add_argument("--expect-values")
    p.set_defaults(fn=cmd_distinct)

    p = with_bounds(with_query(sub.add_parser("dupes")))
    p.set_defaults(fn=cmd_dupes)

    p = with_query(sub.add_parser("absent"))
    p.add_argument("--needles-file", required=True)
    p.add_argument("--min-docs", type=int, default=1)
    p.set_defaults(fn=cmd_absent)

    p = with_bounds(with_query(sub.add_parser("collector-errors")))
    p.set_defaults(fn=cmd_collector_errors)

    p = sub.add_parser("alias")
    p.add_argument("--alias", required=True)
    p.add_argument("--expect-write-index")
    p.set_defaults(fn=cmd_alias)

    p = sub.add_parser("ism")
    p.add_argument("--index", required=True)
    p.add_argument("--policy")
    p.add_argument("--expect-state")
    p.set_defaults(fn=cmd_ism)

    p = sub.add_parser("version")
    p.add_argument("--expect")
    p.set_defaults(fn=cmd_version)

    p = sub.add_parser("snapshot")
    p.add_argument("--out", required=True)
    p.add_argument("--prefix", default="avp-")
    p.set_defaults(fn=cmd_snapshot)

    p = with_query(sub.add_parser("agg"))
    p.add_argument("--by", required=True, help="カンマ区切りのフィールド")
    p.add_argument("--table", action="store_true", help="TSV で出す")
    p.set_defaults(fn=cmd_agg)

    p = sub.add_parser("lag")
    p.add_argument("--max-seconds", type=float)
    p.set_defaults(fn=cmd_lag)

    args = ap.parse_args(argv)
    try:
        result = args.fn(args)
    except AssertFailed as exc:
        print(json.dumps({"cmd": args.cmd, "ok": False, "error": str(exc)}, ensure_ascii=False))
        return 1
    print(json.dumps({"cmd": args.cmd, "ok": True, **result}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
