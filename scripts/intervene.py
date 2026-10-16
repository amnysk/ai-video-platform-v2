#!/usr/bin/env python3
"""介入 API（/v1/intervention）の CLI。HTTP API を呼ぶだけで、DB には触れない。

仕様と運用: docs/operations/intervention-api.md

  export AVP_INTERVENTION_URL=http://localhost:8000      # 省略時はこの値。--url でも指定できる
  export AVP_INTERVENTION_TOKEN="$(cat ~/.avp-intervention-token)"   # token は環境変数だけ

  scripts/intervene.py show EP
  scripts/intervene.py packet EP --format md -o packet.md
  scripts/intervene.py comment EP --body "ここを短く" --segment-id s3
  scripts/intervene.py hold EP --reason "確認したい"
  scripts/intervene.py propose EP --file proposal.json      # ProposeChangePayload の JSON
  scripts/intervene.py import EP --file proposals.json      # ChangeProposalFile の JSON
  scripts/intervene.py propose-storyboard --episode EP --file p.json   # StoryboardPatchProposal
  scripts/intervene.py diff EP --change-set CS              # 差分・影響範囲・見積り
  scripts/intervene.py apply EP --change-set CS --max-cost 0.50
  scripts/intervene.py resume EP
  scripts/intervene.py release-checkpoint EP --point before_upload --manifest-id M
  scripts/intervene.py status COMMAND_ID

command は毎回 uuid4 の command_id を作り、送る前に stderr へ出す。応答が分からないまま切れたら、
**同じ ``--command-id`` で再送する**（同じ内容なら API は保存済みの状態を返し、二重にならない）。
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
import urllib.error
import urllib.parse
import urllib.request
import uuid
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import Any, TextIO

DEFAULT_URL = "http://localhost:8000"
TOKEN_ENV = "AVP_INTERVENTION_TOKEN"
URL_ENV = "AVP_INTERVENTION_URL"
API = "/v1/intervention"
TIMEOUT_SECONDS = 30

#: (method, url, headers, body) -> (status, body)
Transport = Callable[[str, str, dict[str, str], "bytes | None"], "tuple[int, bytes]"]


class TransportError(Exception):
    """HTTP の応答を得られなかった（接続・timeout）。"""


class UsageError(Exception):
    pass


def urllib_transport(
    method: str, url: str, headers: dict[str, str], data: bytes | None
) -> tuple[int, bytes]:
    request = urllib.request.Request(url, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(request, timeout=TIMEOUT_SECONDS) as response:  # noqa: S310
            return response.status, response.read()
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read()
    except (urllib.error.URLError, OSError) as exc:
        raise TransportError(str(getattr(exc, "reason", exc))) from None


# --------------------------------------------------------------------------- 引数


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="intervene.py",
        description="介入 API の CLI（token は環境変数 AVP_INTERVENTION_TOKEN）",
    )
    parser.add_argument("--url", help=f"API の base URL（既定: ${URL_ENV} か {DEFAULT_URL}）")
    sub = parser.add_subparsers(dest="cmd", required=True)

    def episode(name: str, help_text: str) -> argparse.ArgumentParser:
        p = sub.add_parser(name, help=help_text)
        p.add_argument("episode_id")
        return p

    def command(name: str, help_text: str) -> argparse.ArgumentParser:
        p = episode(name, help_text)
        p.add_argument("--command-id", help="再送するときは前回と同じ値（省略時は uuid4）")
        p.add_argument("--expected-control-revision", type=int)
        p.add_argument("--expected-target-revision")
        p.add_argument("--correlation-id")
        return p

    episode("show", "Episode の read model を表示する")
    episode("decisions", "判断記録を表示する")
    episode("commands", "Episode の command を一覧する")
    episode("change-sets", "変更案（ChangeSet）を一覧する")

    p = episode("packet", "ReviewPacket を取り出す")
    p.add_argument("--format", choices=("json", "md", "html"), default="md")
    p.add_argument("-o", "--output", help="書き出し先（省略時は stdout）")

    p = episode("diff", "ChangeSet の差分・影響範囲・見積り、または2つの revision の比較")
    p.add_argument("--change-set")
    p.add_argument("--a", help="比較元 revision_id")
    p.add_argument("--b", help="比較先 revision_id")

    p = command("comment", "コメントを付ける")
    p.add_argument("--body", required=True)
    p.add_argument("--kind", choices=("comment", "review_finding"))
    for anchor in ("revision-id", "manifest-id", "segment-id", "scene-id", "asset-id"):
        p.add_argument(f"--{anchor}")

    p = command("hold", "停止を要求する（新しい有料送信・投稿を止める）")
    p.add_argument("--reason")

    p = command("propose", "変更案を出す（ProposeChangePayload の JSON ファイル）")
    p.add_argument("--file", required=True)

    p = command("apply", "ChangeSet を適用する")
    p.add_argument("--change-set", required=True)
    p.add_argument("--max-cost", type=float, help="承認する見積りの上限（USD）")

    p = command("reject", "ChangeSet を却下する")
    p.add_argument("--change-set", required=True)
    p.add_argument("--reason")

    p = command("resume", "停止した Episode を再開する")
    p.add_argument("--start-stage")

    p = command("release-checkpoint", "待機地点を承認して進める")
    p.add_argument("--point", required=True)
    p.add_argument("--manifest-id", required=True)
    p.add_argument("--note")

    p = sub.add_parser("status", help="command の状態を表示する")
    p.add_argument("command_id")

    p = sub.add_parser(
        "propose-storyboard",
        help="絵コンテの映像文の変更案を出す（StoryboardPatchProposal の JSON。ADR-0043）",
    )
    p.add_argument("--episode", dest="episode_id", required=True)
    p.add_argument("--file", required=True)
    p.add_argument("--command-id", help="再送するときは前回と同じ値（省略時は uuid4）")
    p.add_argument("--expected-control-revision", type=int)

    p = episode("import", "ReviewPacket の変更案ファイル（ChangeProposalFile）を取り込む")
    p.add_argument("--file", required=True)

    sub.add_parser("hash-token", help="$AVP_INTERVENTION_TOKEN の sha256（設定の key）を出す")
    return parser


# --------------------------------------------------------------------------- 組み立て


def _read_json(path: str) -> Any:
    try:
        return json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise UsageError(f"cannot read JSON from {path}: {exc}") from None


def _drop_none(values: Mapping[str, Any]) -> dict[str, Any]:
    return {k: v for k, v in values.items() if v is not None}


def _payload(args: argparse.Namespace) -> tuple[str, dict[str, Any]]:
    cmd = args.cmd
    if cmd == "comment":
        anchor = _drop_none(
            {
                "revision_id": args.revision_id,
                "manifest_id": args.manifest_id,
                "segment_id": args.segment_id,
                "scene_id": args.scene_id,
                "asset_id": args.asset_id,
            }
        )
        return "comment", _drop_none({"anchor": anchor, "body": args.body, "kind": args.kind})
    if cmd == "hold":
        return "request_hold", _drop_none({"reason": args.reason})
    if cmd == "propose":
        return "propose_change", _read_json(args.file)
    if cmd == "apply":
        return "apply_change", _drop_none(
            {"change_set_id": args.change_set, "max_estimated_cost_usd": args.max_cost}
        )
    if cmd == "reject":
        return "reject_change", _drop_none(
            {"change_set_id": args.change_set, "reason": args.reason}
        )
    if cmd == "resume":
        return "resume", _drop_none({"start_stage": args.start_stage})
    if cmd == "release-checkpoint":
        return "release_checkpoint", _drop_none(
            {"point": args.point, "manifest_id": args.manifest_id, "note": args.note}
        )
    raise UsageError(f"unknown command {cmd}")


COMMAND_NAMES = frozenset(
    {"comment", "hold", "propose", "apply", "reject", "resume", "release-checkpoint"}
)


def _request(args: argparse.Namespace) -> tuple[str, str, Any, str | None]:
    """(method, path, json_body, command_id)。"""
    ep = getattr(args, "episode_id", None)
    base = f"{API}/episodes/{urllib.parse.quote(ep, safe='')}" if ep else API
    cmd = args.cmd
    if cmd == "show":
        return "GET", base, None, None
    if cmd in ("decisions", "commands", "change-sets"):
        return "GET", f"{base}/{cmd}", None, None
    if cmd == "packet":
        fmt = {"md": "markdown"}.get(args.format, args.format)
        return "GET", f"{base}/review-packet?format={fmt}", None, None
    if cmd == "diff":
        if args.change_set:
            return "GET", f"{base}/change-sets/{urllib.parse.quote(args.change_set)}", None, None
        if args.a and args.b:
            query = urllib.parse.urlencode({"a": args.a, "b": args.b})
            return "GET", f"{base}/revisions/compare?{query}", None, None
        raise UsageError("diff needs --change-set ID, or both --a REV and --b REV")
    if cmd == "status":
        return "GET", f"{API}/commands/{urllib.parse.quote(args.command_id, safe='')}", None, None
    if cmd == "import":
        return "POST", f"{base}/change-proposals", _read_json(args.file), None
    if cmd == "propose-storyboard":
        command_id = args.command_id or str(uuid.uuid4())
        body = _drop_none(
            {
                "command_id": command_id,
                "expected_control_revision": args.expected_control_revision,
                "proposal": _read_json(args.file),
            }
        )
        return "POST", f"{base}/storyboard/proposals", body, command_id
    if cmd in COMMAND_NAMES:
        command_type, payload = _payload(args)
        command_id = args.command_id or str(uuid.uuid4())
        envelope = _drop_none(
            {
                "command_id": command_id,
                "type": command_type,
                "payload": payload,
                "expected_control_revision": args.expected_control_revision,
                "expected_target_revision": args.expected_target_revision,
                "correlation_id": args.correlation_id,
            }
        )
        return "POST", f"{base}/commands", envelope, command_id
    raise UsageError(f"unknown command {cmd}")


# --------------------------------------------------------------------------- 実行


def _redact(text: str, token: str) -> str:
    return text.replace(token, "<redacted>") if token else text


def main(
    argv: list[str] | None = None,
    *,
    env: Mapping[str, str] | None = None,
    transport: Transport | None = None,
    stdout: TextIO | None = None,
    stderr: TextIO | None = None,
) -> int:
    env = os.environ if env is None else env
    out = stdout or sys.stdout
    err = stderr or sys.stderr
    send = transport or urllib_transport
    args = build_parser().parse_args(argv)

    token = env.get(TOKEN_ENV, "")
    if not token:
        print(f"error: set {TOKEN_ENV} (the token is read only from the environment)", file=err)
        return 2
    if args.cmd == "hash-token":
        print(hashlib.sha256(token.encode("utf-8")).hexdigest(), file=out)
        return 0

    base_url = (args.url or env.get(URL_ENV) or DEFAULT_URL).rstrip("/")
    parsed = urllib.parse.urlparse(base_url)
    if parsed.scheme == "http" and parsed.hostname not in ("localhost", "127.0.0.1", "::1"):
        print("warning: sending the token over plain http to a non-local host", file=err)

    try:
        method, path, body, command_id = _request(args)
    except UsageError as exc:
        print(f"error: {exc}", file=err)
        return 2

    headers = {"Authorization": f"Bearer {token}", "Accept": "application/json"}
    data: bytes | None = None
    if body is not None:
        headers["Content-Type"] = "application/json"
        data = json.dumps(body, ensure_ascii=False).encode("utf-8")
    if command_id is not None:
        print(f"command_id={command_id}", file=err)

    try:
        status, raw = send(method, base_url + path, headers, data)
    except TransportError as exc:
        print(f"error: no response from the API: {_redact(str(exc), token)}", file=err)
        if command_id is not None:
            print(
                "the command may or may not have been received; retry with the same id: "
                f"--command-id {command_id}",
                file=err,
            )
        return 3

    text = _redact(raw.decode("utf-8", errors="replace"), token)
    if args.cmd == "packet" and 200 <= status < 300 and args.output:
        Path(args.output).write_text(text, encoding="utf-8")
        print(f"wrote {args.output}", file=out)
        return 0
    try:
        print(json.dumps(json.loads(text), ensure_ascii=False, indent=2), file=out)
    except json.JSONDecodeError:
        print(text, file=out)
    if not 200 <= status < 300:
        print(f"error: HTTP {status}", file=err)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
