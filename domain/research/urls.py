"""URL の正規化（取得の重複判定。ADR-0037 §8）。純粋関数のみ。

ネットワーク・名前解決をしない。SSRF の判定は取得段（``infrastructure/research/url_guard.py``）が
行い、ここは「同じ資料を 2 回取得しない」ための**キー**だけを作る。正規化が違う 2 つの実装が
あると重複取得（取得枠の浪費）になるので、この関数を唯一の定義にする。
"""

from __future__ import annotations

from urllib.parse import urlsplit, urlunsplit

__all__ = ["normalize_url"]

_DEFAULT_PORTS = {"http": 80, "https": 443}


def normalize_url(url: str) -> str:
    """scheme・host を小文字にし、既定ポート・fragment・空の path を畳んだ URL。

    query は順序を含めて保持する（意味が変わりうるため並べ替えない）。解釈できない URL は
    前後の空白を除いてそのまま返す（正規化できないものを別物として扱う安全側）。
    """
    text = url.strip()
    try:
        parts = urlsplit(text)
        host = parts.hostname
        port = parts.port
    except ValueError:
        return text
    if not parts.scheme or not host:
        return text
    scheme = parts.scheme.lower()
    netloc = host.lower()
    if ":" in netloc:  # IPv6 リテラル
        netloc = f"[{netloc}]"
    if port is not None and port != _DEFAULT_PORTS.get(scheme):
        netloc = f"{netloc}:{port}"
    path = parts.path or "/"
    return urlunsplit((scheme, netloc, path, parts.query, ""))
