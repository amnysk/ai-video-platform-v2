"""秘密に見える値を含むログを出し、stdout・Docker ログ・Fluent Bit バッファ・検索結果の
どこにも残らないことを確かめるための注入器（INV-39、verification-plan の S-SEC）。

値は**実行ごとに乱数で作る**（本物の秘密は使わない。repo にも書かない）。2段で使う::

    # 1) ホスト: 値を作る（0600 のファイル。ディレクトリは repo の外）
    python deploy/logging/test/inject-secrets.py gen --out-dir "$STATE/secrets"
    # 2) 隔離スタックの一時コンテナ（label avp.logging=app）: 値を読んでログに出す
    run-e2e.sh tool --secrets "$STATE/secrets" python deploy/logging/test/inject-secrets.py \
        emit --secrets /run/avp-secrets/secrets.json
    # 3) 検査: needles-logging.txt は stdout / json-file / バッファ / OpenSearch のどこにも無いこと
    #          needles-raw.txt は OpenSearch に無いこと（Collector の Lua の追加防御の範囲）

``emit`` は A の ``infrastructure.logging.configure_logging()`` を使う（未実装なら終了コード 3）。
``--stdlib-only`` は A 実装前に経路（Collector の Lua）だけを試すためのもので、INV-39 の証明には
ならない（標準の basicConfig は伏せ字をしない）。
"""

from __future__ import annotations

import argparse
import base64
import json
import logging
import os
import secrets
import string
import sys
import threading
import uuid
import warnings
from pathlib import Path

_ALNUM = string.ascii_letters + string.digits


def _tok(n: int = 40) -> str:
    return "".join(secrets.choice(_ALNUM) for _ in range(n))


def _b64url(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).decode().rstrip("=")


def generate() -> dict[str, dict[str, str]]:
    """kind -> {"value": ログに載せる文字列, "needle": 残ってはならない核の部分}"""
    pem_body = base64.b64encode(secrets.token_bytes(144)).decode()
    jwt_payload = _b64url(json.dumps({"sub": _tok(12)}).encode())
    jwt_sig = _b64url(secrets.token_bytes(32))
    dsn_pw = _tok(24)
    sig = _tok(48)
    upload_id = _tok(56)
    fal_secret = secrets.token_hex(16)
    long_b64 = base64.b64encode(secrets.token_bytes(240)).decode()
    params_value = _tok(30)
    basic = base64.b64encode(_tok(20).encode()).decode()
    fal_header = secrets.token_hex(16)
    out = {
        "bearer": {"value": f"Bearer {(t := _tok())}", "needle": t},
        "basic": {"value": f"Basic {basic}", "needle": basic},
        "fal_key": {"value": f"{uuid.uuid4()}:{fal_secret}", "needle": fal_secret},
        "fal_header": {"value": f"Key {uuid.uuid4()}:{fal_header}", "needle": fal_header},
        "jwt": {
            "value": f"eyJhbGciOiJIUzI1NiJ9.{jwt_payload}.{jwt_sig}",
            "needle": jwt_sig,
        },
        "pem": {
            "value": f"-----BEGIN PRIVATE KEY-----\n{pem_body}\n-----END PRIVATE KEY-----",
            "needle": pem_body[:64],
        },
        "dsn": {"value": f"postgresql+psycopg://avp:{dsn_pw}@postgres:5432/avp", "needle": dsn_pw},
        "google_access": {"value": f"ya29.{(g := _tok(60))}", "needle": g},
        "google_refresh": {"value": f"1//{(r := _tok(60))}", "needle": r},
        "sk": {"value": f"sk-{(s := _tok(48))}", "needle": s},
        "sqlalchemy_params": {
            "value": f"[parameters: {{'note': '{params_value}'}}]",
            "needle": params_value,
        },
        "long_base64": {"value": long_b64, "needle": long_b64[40:120]},
        "signed_url": {
            "value": f"https://v3.fal.media/files/abc/out.png?X-Amz-Signature={sig}&X-Amz-Credential=x",
            "needle": sig,
        },
        "resumable_session": {
            "value": "https://www.googleapis.com/upload/youtube/v3/videos"
            f"?uploadType=resumable&upload_id={upload_id}",
            "needle": upload_id,
        },
        "userinfo_url": {
            "value": f"https://user:{(u := _tok(20))}@example.invalid/path",
            "needle": u,
        },
        # キー名で伏せる対象（値そのものはパターンに当たらない乱数）
        "keyed": {"value": (k := _tok(32)), "needle": k},
    }
    return out


def cmd_gen(args) -> int:
    out_dir = Path(args.out_dir)
    out_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
    os.umask(0o077)
    data = generate()
    (out_dir / "secrets.json").write_text(json.dumps(data), encoding="utf-8")
    (out_dir / "needles-logging.txt").write_text(
        "".join(v["needle"] + "\n" for v in data.values()), encoding="utf-8"
    )
    # raw（logging を通らない print）は keyed を除く（キー名が無いので Lua も検出できない）
    (out_dir / "needles-raw.txt").write_text(
        "".join(v["needle"] + "\n" for k, v in data.items() if k != "keyed"), encoding="utf-8"
    )
    print(json.dumps({"out_dir": str(out_dir), "kinds": sorted(data)}))
    return 0


def _configure(stdlib_only: bool) -> None:
    if stdlib_only:
        logging.basicConfig(level=logging.DEBUG, stream=sys.stdout)
        return
    try:
        from infrastructure.logging import configure_logging  # type: ignore[import-not-found]
    except ImportError:
        print(
            "inject-secrets: infrastructure.logging.configure_logging が無い（A 未実装）",
            file=sys.stderr,
        )
        raise SystemExit(3) from None
    configure_logging()


def cmd_emit(args) -> int:
    data = json.loads(Path(args.secrets).read_text(encoding="utf-8"))
    tag = args.tag
    _configure(args.stdlib_only)
    log = logging.getLogger("avp.secret_injection")
    v = {k: d["value"] for k, d in data.items()}

    # 1. message 本文にパターン型の秘密
    for kind, value in v.items():
        if kind == "keyed":
            continue
        log.warning("inject %s tag=%s value=%s", kind, tag, value)

    # 2. キー名で伏せる対象（extra の avp 属性・attributes 経由）
    log.info(
        "inject keyed tag=%s",
        tag,
        extra={
            "avp": {
                "request_id": tag,
                "attributes": {
                    "api_key": v["keyed"],
                    "authorization": v["keyed"],
                    "cookie": v["keyed"],
                    "password": v["keyed"],
                    "upload_url": v["keyed"],
                    "nested": {"refresh_token": v["keyed"], "code": v["keyed"]},
                },
            }
        },
    )

    # 3. 例外メッセージと stack（chain を含む）
    try:
        try:
            raise ValueError(f"inner {v['dsn']} {v['sqlalchemy_params']}")
        except ValueError as inner:
            raise RuntimeError(f"outer {v['bearer']} {v['signed_url']}") from inner
    except RuntimeError:
        log.exception("inject exception tag=%s", tag)

    # 4. 第三者 logger（httpx の URL・uvicorn・sqlalchemy の名前で）
    logging.getLogger("httpx").warning('HTTP Request: GET %s "HTTP/1.1 403"', v["signed_url"])
    logging.getLogger("sqlalchemy.engine").warning("%s", v["sqlalchemy_params"])
    logging.getLogger("temporalio.core").warning("core %s", v["google_access"])

    # 5. warnings と未捕捉例外（thread）
    logging.captureWarnings(True)
    warnings.warn(f"inject warning {v['sk']}", stacklevel=1)

    def boom() -> None:
        raise RuntimeError(f"thread {v['jwt']}")

    t = threading.Thread(target=boom)
    t.start()
    t.join()

    # 6. logging を通らない出力（INV-39 の対象外。Collector の Lua の追加防御だけが頼り）
    if args.raw:
        for kind, value in v.items():
            if kind == "keyed":
                continue
            print(f"raw-stdout inject {kind} tag={tag} value={value}", flush=True)
            print(f"raw-stderr inject {kind} tag={tag} value={value}", file=sys.stderr, flush=True)

    logging.shutdown()
    return 0


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawTextHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("gen")
    p.add_argument("--out-dir", required=True)
    p.set_defaults(fn=cmd_gen)
    p = sub.add_parser("emit")
    p.add_argument("--secrets", required=True)
    p.add_argument("--tag", default="secret-injection")
    p.add_argument("--raw", action="store_true", help="logging を通らない print も出す")
    p.add_argument("--stdlib-only", action="store_true")
    p.set_defaults(fn=cmd_emit)
    args = ap.parse_args(argv)
    return args.fn(args)


if __name__ == "__main__":
    raise SystemExit(main())
