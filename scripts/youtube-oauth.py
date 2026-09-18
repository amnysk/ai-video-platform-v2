#!/usr/bin/env python3
"""YouTube 用 refresh token を取得する（手動・一回限り。テストからは呼ばない）。

installed app の loopback + PKCE（developers.google.com/identity/protocols/oauth2/native-app）。
scope は youtube.upload・youtube.readonly・yt-analytics.readonly
（Topic Planner / ADR-0025）だけ。refresh token は **repo 外** のファイルへ
0600 で書き、画面にもログにも出さない。

    YOUTUBE_CLIENT_ID=... YOUTUBE_CLIENT_SECRET=... \\
        .venv/bin/python scripts/youtube-oauth.py --out ~/.config/avp/youtube-refresh-token
"""

from __future__ import annotations

import argparse
import base64
import getpass
import hashlib
import http.server
import os
import pathlib
import secrets
import sys
import threading
import urllib.parse
import webbrowser

import httpx

AUTH_ENDPOINT = "https://accounts.google.com/o/oauth2/v2/auth"
TOKEN_ENDPOINT = "https://oauth2.googleapis.com/token"
SCOPES = (
    "https://www.googleapis.com/auth/youtube.upload",
    "https://www.googleapis.com/auth/youtube.readonly",
    "https://www.googleapis.com/auth/yt-analytics.readonly",
)
REPO_ROOT = pathlib.Path(__file__).resolve().parents[1]


def _refuse_inside_repo(path: pathlib.Path) -> pathlib.Path:
    resolved = path.expanduser().resolve()
    root = REPO_ROOT.resolve()
    if resolved == root or root in resolved.parents:
        sys.exit("refusing to write the refresh token inside the repository")
    return resolved


def missing_scopes(granted: object) -> list[str]:
    """token 応答の ``scope``（空白区切り）に無い、要求した scope。

    同意画面で項目の選択を外されると一部だけ付く（granular consent）。
    """
    have = set(granted.split()) if isinstance(granted, str) else set()
    return [s for s in SCOPES if s not in have]


def _pkce() -> tuple[str, str]:
    verifier = base64.urlsafe_b64encode(secrets.token_bytes(48)).rstrip(b"=").decode()
    challenge = (
        base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
    )
    return verifier, challenge


def _wait_for_code(expected_state: str) -> tuple[str, int, threading.Event, dict[str, str]]:
    result: dict[str, str] = {}
    done = threading.Event()

    class Handler(http.server.BaseHTTPRequestHandler):
        def do_GET(self) -> None:  # noqa: N802
            query = dict(urllib.parse.parse_qsl(urllib.parse.urlparse(self.path).query))
            if query.get("state") != expected_state:
                self.send_response(400)
                self.end_headers()
                return
            result.update(query)
            self.send_response(200)
            self.send_header("Content-Type", "text/plain; charset=utf-8")
            self.end_headers()
            self.wfile.write(b"Authorization received. You can close this tab.")
            done.set()

        def log_message(self, format: str, *args: object) -> None:  # noqa: A002
            return  # URL に code が載るので記録しない

    server = http.server.HTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return "127.0.0.1", server.server_address[1], done, result


def _write_token(path: pathlib.Path, token: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    try:
        os.fchmod(fd, 0o600)
        os.write(fd, token.encode("utf-8"))
    finally:
        os.close(fd)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out", required=True, help="refresh token file (outside the repo)")
    parser.add_argument("--no-browser", action="store_true")
    args = parser.parse_args()
    out = _refuse_inside_repo(pathlib.Path(args.out))

    client_id = os.environ.get("YOUTUBE_CLIENT_ID") or input("OAuth client id: ").strip()
    client_secret = os.environ.get("YOUTUBE_CLIENT_SECRET") or getpass.getpass(
        "OAuth client secret: "
    )
    verifier, challenge = _pkce()
    state = secrets.token_urlsafe(24)
    host, port, done, result = _wait_for_code(state)
    redirect_uri = f"http://{host}:{port}"
    url = (
        AUTH_ENDPOINT
        + "?"
        + urllib.parse.urlencode(
            {
                "client_id": client_id,
                "redirect_uri": redirect_uri,
                "response_type": "code",
                "scope": " ".join(SCOPES),
                "access_type": "offline",
                "prompt": "consent",
                "code_challenge": challenge,
                "code_challenge_method": "S256",
                "state": state,
            }
        )
    )
    print("Open this URL and approve access:\n" + url, file=sys.stderr)
    if not args.no_browser:
        webbrowser.open(url)
    if not done.wait(timeout=600):
        sys.exit("timed out waiting for the authorization redirect")
    if "code" not in result:
        sys.exit(f"authorization failed: {result.get('error', 'unknown')}")

    response = httpx.post(
        TOKEN_ENDPOINT,
        data={
            "client_id": client_id,
            "client_secret": client_secret,
            "code": result["code"],
            "code_verifier": verifier,
            "grant_type": "authorization_code",
            "redirect_uri": redirect_uri,
        },
        timeout=30,
    )
    body = (
        response.json()
        if response.headers.get("content-type", "").startswith("application/json")
        else {}
    )
    token = body.get("refresh_token") if isinstance(body, dict) else None
    if response.status_code != 200 or not isinstance(token, str):
        sys.exit(f"token exchange failed: HTTP {response.status_code} {body.get('error', '')}")
    missing = missing_scopes(body.get("scope"))
    if missing:
        # 部分的な token は書かない（upload・Analytics のどちらかが 403 になる）
        names = ", ".join(m.rsplit("/", 1)[-1] for m in missing)
        sys.exit(f"scope missing: {names}; re-run and approve every requested permission")
    _write_token(out, token)
    print(f"refresh token written to {out} (0600)", file=sys.stderr)


if __name__ == "__main__":
    main()
