"""契約どおりの JSON ログ行（と、わざと壊した行）を stdout に出す試験用の発生器。

A（アプリのログ実装）に依存せず、B（Fluent Bit / OpenSearch）の経路だけを試すために使う。
隔離 app スタックの ``tool`` サービス（label ``avp.logging=app``）から起動する::

    run-e2e.sh tool python deploy/logging/test/loggen.py --tag rot-1 --count 5000

出す行は ``request_id=<tag>`` を持つので、検索側は ``request_id`` の完全一致で数えられる
（``search-assert.py count --term request_id=<tag>``）。``attributes.seq`` に通番。
標準ライブラリだけを使う（コンテナでもホストでも動く）。
"""

from __future__ import annotations

import argparse
import json
import sys
import time
import uuid
from datetime import UTC, datetime

try:  # 語彙の唯一の宣言元（AGENTS.md §8）。/src を PYTHONPATH に持つコンテナでは読める
    from contracts.log_contract import LOG_SCHEMA_VERSION
except ImportError:  # pragma: no cover - 単体でコピーして使う場合
    LOG_SCHEMA_VERSION = 1


def _now() -> str:
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"


def valid_event(tag: str, seq: int, pad: int) -> dict:
    return {
        "@timestamp": _now(),
        "schema_version": LOG_SCHEMA_VERSION,
        "event_id": uuid.uuid4().hex,
        "event_name": "log.record",
        "level": "INFO",
        "message": f"loggen seq={seq}" + (" " + "x" * pad if pad else ""),
        "service_name": "loggen",
        "environment": "test",
        "git_sha": "unknown",
        "logger": "avp.loggen",
        "request_id": tag,
        "attributes": {"seq": seq},
    }


#: Bulk の部分失敗・型不整合・Collector の退避を起こす行（kind, 行を作る関数）
def malformed_lines(tag: str) -> list[tuple[str, str]]:
    base = valid_event(tag, -1, 0)

    def variant(**over) -> str:
        return json.dumps({**base, "event_id": uuid.uuid4().hex, **over}, ensure_ascii=False)

    return [
        ("not_json", "loggen: this line is not JSON {"),
        ("truncated_json", '{"@timestamp": "2026-09-30T00:00:00.000Z", "event_name": "log.rec'),
        ("json_array", "[1, 2, 3]"),
        ("http_status_string", variant(http_status="forbidden")),
        ("retryable_string", variant(retryable="yes")),
        ("scene_revision_float_string", variant(scene_revision="1.5x")),
        ("duration_ms_object", variant(duration_ms={"v": 1})),
        ("timestamp_garbage", variant(**{"@timestamp": "not-a-date"})),
        ("timestamp_missing", json.dumps({k: v for k, v in base.items() if k != "@timestamp"})),
        ("keyword_object", variant(episode_id={"nested": "x"})),
        ("unknown_top_level", variant(totally_unknown_field="x", provider_body={"a": 1})),
        ("error_code_array", variant(error_code=["file_download_error", "content_policy"])),
    ]


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--tag", required=True, help="request_id に入れる識別子（検索の鍵）")
    ap.add_argument("--count", type=int, default=1000)
    ap.add_argument("--rate", type=float, default=0.0, help="1秒あたりの行数（0 は全速）")
    ap.add_argument("--pad", type=int, default=0, help="message に足す埋め草（bytes）")
    ap.add_argument("--malformed", action="store_true", help="valid の間に壊れた行を混ぜる")
    ap.add_argument(
        "--duplicate-every",
        type=int,
        default=0,
        help="K 行ごとに直前の行を同じ event_id のまま再出力（文書 ID による重複抑制の試験）",
    )
    ap.add_argument(
        "--long-line", type=int, default=0, help="この bytes 数の1行を出す（skip_long_lines 試験）"
    )
    args = ap.parse_args(argv)

    out = sys.stdout
    interval = 1.0 / args.rate if args.rate > 0 else 0.0
    kinds = len(malformed_lines(args.tag))
    for seq in range(args.count):
        line = json.dumps(valid_event(args.tag, seq, args.pad), ensure_ascii=False)
        out.write(line + "\n")
        if args.duplicate_every and seq % args.duplicate_every == 0:
            out.write(line + "\n")
        if args.malformed and seq % 10 == 5:
            # 毎回作り直す（同じ event_id の再送＝409 と区別するため）
            kind, line = malformed_lines(args.tag)[(seq // 10) % kinds]
            out.write(line + "\n")
            sys.stderr.write(f"loggen malformed kind={kind}\n")
        if interval:
            out.flush()
            time.sleep(interval)
    if args.long_line:
        event = valid_event(args.tag, -2, args.long_line)
        out.write(json.dumps(event) + "\n")
    out.flush()
    sys.stderr.write(f"loggen done tag={args.tag} valid={args.count}\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
