"""信頼できない文字列（LLM 生成の地名・ラベル・主張・自由文）を結果・ログに残さない（INV-106）。

検査結果・レポート・ログが運んでよいのは、**理由コード（enum）・対象 id・検査した値（数値・enum）・
証拠への参照（id / hash）**だけ。入力の文字列そのものは運ばない。「どの値か」を辿りたいときは
``untrusted_ref``（長さと sha256 の先頭）で参照する。定義は**ここだけ**（AGENTS.md §8）。
"""

from __future__ import annotations

import hashlib
import re

#: 検査結果が運ぶ id（地点・主張・資料・参照地点・証拠）。英数字で始まり、記号は ``_ . : -`` だけ。
SAFE_ID_PATTERN = r"^[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}$"
#: 検査名（``<place_id>.<check>`` / ``label[<place_id>]``）。
CHECK_NAME_PATTERN = r"^[A-Za-z0-9][A-Za-z0-9_.:\[\]-]{0,199}$"
#: ``untrusted_ref`` の形。
UNTRUSTED_REF_PATTERN = r"^len=[0-9]{1,9}:sha256=[0-9a-f]{12}$"
#: 結果の ``detail`` に入れてよい文字: 印字可能な ASCII（改行・制御文字・非 ASCII を含まない）。
SAFE_DETAIL_PATTERN = r"^[\x20-\x7e]*$"
MAX_DETAIL_CHARS = 500

_SAFE_ID = re.compile(SAFE_ID_PATTERN)
_SAFE_DETAIL = re.compile(SAFE_DETAIL_PATTERN)


def untrusted_ref(value: object) -> str:
    """信頼できない値の参照。**値は写さない**: 型に依らず長さと sha256 の先頭 12 桁だけ。"""
    text = value if isinstance(value, str) else repr(type(value).__name__)
    digest = hashlib.sha256(text.encode("utf-8", "replace")).hexdigest()[:12]
    return f"len={len(text)}:sha256={digest}"


def is_safe_id(value: object) -> bool:
    return isinstance(value, str) and _SAFE_ID.fullmatch(value) is not None


def is_safe_detail(value: object) -> bool:
    return (
        isinstance(value, str)
        and len(value) <= MAX_DETAIL_CHARS
        and _SAFE_DETAIL.fullmatch(value) is not None
    )
