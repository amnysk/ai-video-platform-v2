"""Codex の model 指定に関する語彙（唯一の宣言元。AGENTS.md §8）。

**どの model が使えるかをここに書かない。** ChatGPT アカウントで使える model は
アカウント・プラン・CLI 版で変わり、CLI のローカルキャッシュ（models_cache.json）にあっても
サーバーに拒否される（実測 2026-10: gpt-6-sol がキャッシュ上は list なのに 400）。
使える集合は運用者が ``CODEX_ALLOWED_MODELS`` に実測してから書く。空なら「未検証」。
"""

from __future__ import annotations

import re

#: 許可 model をカンマ区切りで渡す環境変数。空なら検査しない（警告だけ）
ALLOWED_MODELS_ENV = "CODEX_ALLOWED_MODELS"

#: codex の 400 本文: ``The 'X' model is not supported when using Codex with a ChatGPT account.``
_UNSUPPORTED_MODEL_RE = re.compile(r"The '([^']+)' model is not supported", re.IGNORECASE)


def extract_unsupported_model(text: str) -> str | None:
    """拒否メッセージから model 名を取り出す。該当しなければ ``None``。"""
    match = _UNSUPPORTED_MODEL_RE.search(text.replace('\\"', '"'))
    return match.group(1) if match else None


def parse_allowlist(raw: str) -> frozenset[str]:
    return frozenset(part.strip() for part in raw.split(",") if part.strip())
