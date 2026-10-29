"""API の bearer token ファイルの読み取りと検査（ADR-0080 §B）。

API と release preflight が共有する唯一の実装。
形式: 1 行 1 件 ``<actor>:<token>``（actor は ``owner`` / ``agent``。空行と ``#`` 行は無視。
token は 32 文字以上）。
ファイルは通常ファイルで 0600（group / others が読めない）であること。どんな不備でも例外にせず、
``problem`` に理由を入れて返す（呼び出し側は「無い = 全拒否」に倒す）。
"""

from __future__ import annotations

import os
import stat
from dataclasses import dataclass

TOKEN_MIN_LENGTH = 32
ACTOR_NAMES = frozenset({"owner", "agent"})


@dataclass(frozen=True, slots=True)
class TokenFile:
    """読み取り結果。``problem`` が None のときだけ ``entries`` を信用する。"""

    path: str
    exists: bool
    problem: str | None
    entries: tuple[tuple[str, str], ...] = ()
    mode: int | None = None
    uid: int | None = None


def read_token_file(path: str | None) -> TokenFile:
    if not path:
        return TokenFile("", False, "no path configured")
    try:
        info = os.stat(path)
    except FileNotFoundError:
        return TokenFile(path, False, "absent")
    except OSError:
        return TokenFile(path, False, "not readable")
    mode, uid = stat.S_IMODE(info.st_mode), info.st_uid
    if not stat.S_ISREG(info.st_mode):
        return TokenFile(path, True, "not a regular file", mode=mode, uid=uid)
    if mode & 0o077:
        return TokenFile(path, True, "readable by group/others (must be 0600)", mode=mode, uid=uid)
    try:
        with open(path, encoding="utf-8") as handle:
            lines = handle.read().splitlines()
    except (OSError, UnicodeDecodeError):
        return TokenFile(path, True, "not readable", mode=mode, uid=uid)
    entries: list[tuple[str, str]] = []
    for raw in lines:
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        name, sep, token = line.partition(":")
        name, token = name.strip(), token.strip()
        if name not in ACTOR_NAMES:
            return TokenFile(path, True, "unknown actor", mode=mode, uid=uid)
        if not sep or len(token) < TOKEN_MIN_LENGTH:
            return TokenFile(path, True, "malformed or too short token", mode=mode, uid=uid)
        entries.append((name, token))
    if not entries:
        return TokenFile(path, True, "no tokens", mode=mode, uid=uid)
    return TokenFile(path, True, None, tuple(entries), mode, uid)


__all__ = ["ACTOR_NAMES", "TOKEN_MIN_LENGTH", "TokenFile", "read_token_file"]
