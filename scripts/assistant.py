#!/usr/bin/env python3
"""対話 AI アシスタント向け tool の CLI（ADR-0042 §7）。介入 HTTP API を呼ぶだけ（DB に触れない）。

  scripts/assistant.py tools                      # tool の定義（JSON）。効果・必要 scope 付き
  scripts/assistant.py tools --tool-use           # LLM の tool use にそのまま渡せる形
  scripts/assistant.py call get_episode_status --input '{"episode_id": "..."}' \\
      --session chat-2026-10-15 --tool-call-id call-1

接続は ``scripts/intervene.py`` と同じ環境変数: ``AVP_INTERVENTION_URL``（省略時
http://localhost:8000）と ``AVP_INTERVENTION_TOKEN``（token は環境変数だけ。表示しない）。
``--input -`` は標準入力から読む。結果は JSON を標準出力へ。

終了コード: 0 = ok / submitted、1 = サーバの拒否・要確認（cost_changed / needs_human_approval 等）、
2 = 使い方・入力・token 未設定、3 = 応答なし（command は同じ ``--tool-call-id`` で再試行する）。
運用は docs/operations/assistant.md。
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from collections.abc import Mapping
from pathlib import Path
from typing import Any, TextIO

import httpx

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from apps.assistant.client import InterventionClient  # noqa: E402
from apps.assistant.tools import execute_tool  # noqa: E402
from contracts.assistant_tools import (  # noqa: E402
    DEFAULT_URL,
    TOKEN_ENV,
    URL_ENV,
    catalogue,
    tool_use_definitions,
)

EXIT_OK = 0
EXIT_REFUSED = 1
EXIT_USAGE = 2
EXIT_NO_RESPONSE = 3
_SUCCESS = frozenset({"ok", "submitted"})
_USAGE = frozenset({"invalid_input", "unknown_tool"})


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="assistant.py",
        description=f"tool adapter の CLI（token は環境変数 {TOKEN_ENV}、URL は {URL_ENV}）",
    )
    sub = parser.add_subparsers(dest="cmd", required=True)
    tools = sub.add_parser("tools", help="tool の定義を JSON で出す")
    tools.add_argument(
        "--tool-use", action="store_true", help="name / description / input_schema だけにする"
    )
    call = sub.add_parser("call", help="tool を1回実行する")
    call.add_argument("tool")
    call.add_argument("--input", required=True, help="JSON object。'-' で標準入力")
    call.add_argument("--session", required=True, help="アシスタントの会話／セッション ID")
    call.add_argument(
        "--tool-call-id", required=True, help="この tool 呼び出しの ID（再試行は同じ値）"
    )
    call.add_argument("--url", help=f"API の base URL（既定: ${URL_ENV} か {DEFAULT_URL}）")
    return parser


def _exit_code(result: Mapping[str, Any]) -> int:
    status = result.get("status")
    if status in _SUCCESS:
        return EXIT_OK
    if status in _USAGE:
        return EXIT_USAGE
    if status == "transport_error":
        return EXIT_NO_RESPONSE
    return EXIT_REFUSED


def main(
    argv: list[str] | None = None,
    *,
    env: Mapping[str, str] | None = None,
    stdin: TextIO | None = None,
    stdout: TextIO | None = None,
    stderr: TextIO | None = None,
    transport: httpx.BaseTransport | None = None,
) -> int:
    env = os.environ if env is None else env
    stdin = sys.stdin if stdin is None else stdin
    out = sys.stdout if stdout is None else stdout
    err = sys.stderr if stderr is None else stderr
    try:
        args = build_parser().parse_args(argv)
    except SystemExit as exc:
        return EXIT_USAGE if exc.code else EXIT_OK
    if args.cmd == "tools":
        data = tool_use_definitions() if args.tool_use else catalogue()
        out.write(json.dumps(data, ensure_ascii=False, indent=2) + "\n")
        return EXIT_OK
    token = env.get(TOKEN_ENV, "")
    if not token:
        err.write(f"{TOKEN_ENV} is not set (the token is read from the environment only)\n")
        return EXIT_USAGE
    raw = stdin.read() if args.input == "-" else args.input
    try:
        arguments = json.loads(raw)
    except json.JSONDecodeError:
        err.write("--input is not valid JSON\n")
        return EXIT_USAGE
    client = InterventionClient(
        args.url or env.get(URL_ENV) or DEFAULT_URL,
        token,
        session_id=args.session,
        transport=transport,
    )
    try:
        result = execute_tool(client, args.tool, arguments, tool_call_id=args.tool_call_id)
    finally:
        client.close()
    out.write(json.dumps(result, ensure_ascii=False, indent=2) + "\n")
    return _exit_code(result)


if __name__ == "__main__":
    raise SystemExit(main())
