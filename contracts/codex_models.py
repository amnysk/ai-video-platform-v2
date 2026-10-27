"""Codex の model 指定に関する語彙（唯一の宣言元。AGENTS.md §8）。

**どの model が使えるかをここに書かない。** ChatGPT アカウントで使える model は
アカウント・プラン・CLI 版で変わり、CLI のローカルキャッシュ（models_cache.json）にあっても
サーバーに拒否される（実測 2026-10: gpt-6-sol がキャッシュ上は list なのに 400）。
使える集合は運用者が ``CODEX_ALLOWED_MODELS`` に実測してから書く。空なら「未検証」。
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from enum import StrEnum

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


# ----------------------------------------------- 事前診断（ADR-0058 / INV-75、単一宣言元）

#: model を ``CODEX_MODEL`` で明示することを必須にするか。既定は False（従来どおり config.toml に
#: 従う）。所有者が実測で model を決めて固定したあと True にすると、固定を忘れた構成は
#: 日次の入口で止まる（共有の ``~/.codex/config.toml`` の書き換えが本番に伝わらない）
REQUIRE_PINNED_ENV = "CODEX_REQUIRE_PINNED_MODEL"

#: この時間より古い models_cache.json は「無い model」の証拠にしない
CATALOG_MAX_AGE_HOURS = 24 * 7

#: 日次の入口で model を確認する Activity（script worker の queue。Codex の設定を持つ側）
CODEX_MODEL_PREFLIGHT_ACTIVITY = "codex_model_preflight"


class CodexModelVerdict(StrEnum):
    """事前診断の結果。**OK は「許可リストにあり、catalog にもある」ときだけ**。"""

    OK = "ok"
    #: catalog にはある（または model が分からない）が、実測された証拠が無い
    UNVERIFIED = "unverified"
    #: catalog を読めない / 古い。「無い」とは言えない
    CATALOG_UNREADABLE = "catalog_unreadable"
    #: catalog に無い model。サーバーも拒否する（実行しても必ず失敗する）
    NOT_IN_CATALOG = "not_in_catalog"
    #: 許可リスト（実測した集合）に無い
    NOT_ALLOWED = "not_allowed"
    #: ``CODEX_REQUIRE_PINNED_MODEL`` なのに ``CODEX_MODEL`` が空
    NOT_PINNED = "not_pinned"


#: これらが出たら日次は Codex を呼ばずに止まる（必ず失敗する構成だから）
BLOCKING_VERDICTS: frozenset[CodexModelVerdict] = frozenset(
    {
        CodexModelVerdict.NOT_IN_CATALOG,
        CodexModelVerdict.NOT_ALLOWED,
        CodexModelVerdict.NOT_PINNED,
    }
)


@dataclass
class CodexPreflightRequest:
    slot_date: str = ""


@dataclass
class CodexPreflightResult:
    verdict: str
    blocking: bool
    #: 実効 model（``CODEX_MODEL`` > config.toml）。分からなければ None
    model: str | None = None
    #: "env" | "config.toml" | None
    source: str | None = None
    message: str = ""
