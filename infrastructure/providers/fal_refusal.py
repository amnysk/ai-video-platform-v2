"""fal の 401/402/403（有料呼び出しの拒否）の分類と本文要約（ADR-0057）。

fal 固有の判定語はこの module **だけ**が持つ（INV-6 / AGENTS.md §8）。queue（submit）と
storage（準備の upload / token）の両方が同じ関数を使う。

**公式文書に残高不足の構造化コードは無い**（ADR-0057 に取得日・URL・引用を残した）:

- https://fal.ai/docs/model-apis/errors は 400/422/499/500/502/503/504 だけを列挙し、
  401/402/403 も残高も書かない。未掲載の ``type`` は「非リトライの 422 として扱い ``msg`` を見よ」。
- https://fal.ai/docs/model-apis/faq は「残高がロックしきい値を下回るとアカウントは
  ロックされ API リクエストは拒否される」と書くが、HTTP status・本文の形は書かない。
- https://fal.ai/docs/model-apis/authentication は 401/403 を書かない。

したがって **HTTP status だけでは残高不足を決めない**。本文の印（``BALANCE_BODY_MARKERS``）が
あるときだけ ``BALANCE_EXHAUSTED``。印の出所は ``source`` に残す（公式 URL か ``observed:``）。
印が無い 402/403 は ``UNKNOWN``（安全側: breaker を開き、人が原因を確かめる）。401 は印が無ければ
``AUTH_INVALID``（HTTP の意味論 RFC 9110 §15.5.2。fal 固有の文書ではない）。
"""

from __future__ import annotations

import re
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any

from contracts.provider_health import RefusalKind
from domain.errors import (
    ProviderBalanceExhaustedError,
    ProviderRefusalUnknownError,
    ProviderUnavailableError,
)
from infrastructure.logging.redaction import sanitize_text

#: 要約の上限（例外メッセージ・台帳の error_summary・breaker の evidence に載せる）
SUMMARY_MAX_CHARS = 300
_NO_BODY = "(no response body)"


@dataclass(frozen=True, slots=True)
class BalanceMarker:
    """残高不足を示す本文の印（小文字の部分一致）。出所を必ず持つ。"""

    text: str
    #: 公式 URL（``https://``）か、公式に形式が無い印の観測元（``observed:``）
    source: str


#: 残高不足の印の**唯一の定義**。増やすときは出所（source）と ADR-0057 の記録を同時に更新する。
#: 公式 docs はロックの存在（faq）を述べるが本文の形を述べない。以下は運用者が観測した本文で、
#: 公式の構造化コードではない（確定の根拠としては弱い）。弱い根拠でも安全側に倒れる:
#: 結果は「breaker を開いて人の再開承認を待つ」で、UNKNOWN と同じ止まり方になる。
BALANCE_BODY_MARKERS: tuple[BalanceMarker, ...] = (
    BalanceMarker("exhausted balance", "observed:operator-report-2026-10 (fal FAQ documents lock)"),
    BalanceMarker("user is locked", "observed:operator-report-2026-10 (fal FAQ documents lock)"),
)


@dataclass(frozen=True, slots=True)
class RefusalClassification:
    kind: RefusalKind
    #: 一致した印の text（無ければ None）。ADR-0057 の記録・テストが参照する
    marker: str | None = None


def _texts(body: Any, depth: int = 0) -> list[str]:
    """本文から人間向けの文字列だけを集める（detail / message / error / msg。再帰は浅く）。"""
    if depth > 4 or body is None:
        return []
    if isinstance(body, str):
        return [body]
    if isinstance(body, list):
        return [t for item in body[:20] for t in _texts(item, depth + 1)]
    if isinstance(body, dict):
        out: list[str] = []
        for key in ("detail", "message", "error", "msg", "reason"):
            if key in body:
                out.extend(_texts(body[key], depth + 1))
        return out
    return []


def classify_refusal(status: int, body: Any) -> RefusalClassification:
    """401/402/403 の分類。HTTP status だけで残高不足とは決めない。"""
    # 空白の連続・改行・タブは 1 個のスペースに畳んでから照合する（書式揺れで UNKNOWN にしない）
    haystack = _WS_RE.sub(" ", " ".join(_texts(body)).lower())
    for marker in BALANCE_BODY_MARKERS:
        if _WS_RE.sub(" ", marker.text) in haystack:
            return RefusalClassification(RefusalKind.BALANCE_EXHAUSTED, marker.text)
    if status == 401:
        return RefusalClassification(RefusalKind.AUTH_INVALID)
    return RefusalClassification(RefusalKind.UNKNOWN)


_WS_RE = re.compile(r"\s+")

#: query を落とす URL の形: ``scheme://host/path`` / scheme なし ``//host/path`` / 裸の
#: ``host.tld/path``。署名付き URL は scheme が無くても query に署名を持つ。ドットを含まない
#: 普通の文（``why?``）は URL とみなさない。
_URL_QUERY_RE = re.compile(
    r"((?:[a-z][a-z0-9+.-]*:)?//[^\s?#\"'<>]+"
    r"|\b(?:[a-z0-9-]+\.)+[a-z]{2,}(?::\d+)?(?:/[^\s?#\"'<>]*)?)"
    r"\?[^\s\"'<>]*",
    re.IGNORECASE,
)


def summarize_refusal_body(body: Any, *, secrets: Sequence[str] = ()) -> str:
    """401/402/403 の応答本文の要約。key・token・署名付き URL・query を残さない。

    ``secrets`` は呼び出し側が知っている値（API キー）。パターンに一致しない形でも消す。
    """
    texts = _texts(body)
    if not texts:
        return _NO_BODY
    text = " ".join(t.strip() for t in texts if t.strip())
    for secret in secrets:
        if secret:
            text = text.replace(secret, "[REDACTED]")
    text = _URL_QUERY_RE.sub(r"\1", text)
    text, _ = sanitize_text(text)
    text = re.sub(r"\s+", " ", text).strip()
    return text[:SUMMARY_MAX_CHARS] if text else _NO_BODY


def refusal_error(
    status: int, body: Any, *, what: str, secrets: Sequence[str] = ()
) -> ProviderBalanceExhaustedError | ProviderRefusalUnknownError | ProviderUnavailableError:
    """401/402/403 を例外にする。本文の要約（secret なし）を必ずメッセージに残す。"""
    classification = classify_refusal(status, body)
    summary = summarize_refusal_body(body, secrets=secrets)
    kind = classification.kind
    if kind is RefusalKind.BALANCE_EXHAUSTED:
        return ProviderBalanceExhaustedError(
            f"fal {what} refused: HTTP {status} balance exhausted (marker "
            f"{classification.marker!r}): {summary}",
            http_status=status,
            summary=summary,
        )
    if kind is RefusalKind.AUTH_INVALID:
        return ProviderUnavailableError(
            f"fal {what} refused: HTTP {status} unauthorized: {summary}", http_status=status
        )
    return ProviderRefusalUnknownError(
        f"fal {what} refused: HTTP {status} reason unconfirmed: {summary}",
        http_status=status,
        summary=summary,
    )


__all__ = [
    "BALANCE_BODY_MARKERS",
    "SUMMARY_MAX_CHARS",
    "BalanceMarker",
    "RefusalClassification",
    "classify_refusal",
    "refusal_error",
    "summarize_refusal_body",
]
