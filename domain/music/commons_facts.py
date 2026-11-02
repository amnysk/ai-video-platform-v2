"""Wikimedia Commons の MediaWiki API 応答 → ``LicenseFacts``（ADR-0087 §2、純関数）。

- 入力は**保存する証拠そのもの**（``build_evidence_payload`` の payload）。
  判定は保存した証拠からいつでも再現できる。
  証拠の sha256 は payload の正準 JSON（``canonical_json_bytes``）の sha256
- 推定を入れない。欠けている項目は ``None``、HTML は決定的に除去する
- 危険なマークアップ（script / style / iframe / コメント）を含む値は、除去後の文字列に目印を付けて
  「きれいな Own work」に潰れないようにする（判定側の完全一致を通らない）
"""

from __future__ import annotations

import html
import re
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from contracts.music_sources import LicenseFacts, MusicSourceId
from domain.artifact.hashing import canonical_json_bytes, sha256_hex

EVIDENCE_SCHEMA = "commons-evidence-1"
COMMONS_PAGE_URL_PREFIX = "https://commons.wikimedia.org/wiki/"
UNSAFE_MARKUP_MARKER = "[unsafe-markup]"

_UNSAFE = re.compile(r"<\s*(script|style|iframe|object|embed)\b|<!--|\bon\w+\s*=", re.IGNORECASE)
_TAG = re.compile(r"<[^>]*>")
_SPACE = re.compile(r"\s+")

#: 削除・係争を示すカテゴリ名 / テンプレート名の断片（小文字・アンダースコアは空白に）
_DISPUTE_FRAGMENTS = (
    "deletion",
    "copyright violation",
    "copyvio",
    "no permission",
    "no source",
    "no license",
    "speedy",
    "dmca",
    "disputed",
    "possible copyright",
    "license review",
    "problematic",
)
_DISPUTE_TEMPLATES = frozenset(
    {"delete", "copyvio", "speedy", "npd", "nsd", "nld", "no permission since", "dr", "puf", "ncd"}
)


def strip_html(value: str | None) -> str | None:
    """HTML を決定的に文字列へ。危険なマークアップは目印を付ける。"""
    if value is None:
        return None
    unsafe = bool(_UNSAFE.search(value))
    text = html.unescape(_TAG.sub("", value))
    text = _SPACE.sub(" ", text).strip()
    return f"{text} {UNSAFE_MARKUP_MARKER}".strip() if unsafe else text


def pages_from_response(response: Mapping[str, Any]) -> list[dict[str, Any]]:
    """``query.pages``（formatversion 1 の dict / 2 の list の両方）を list にする。"""
    query = response.get("query")
    if not isinstance(query, Mapping):
        return []
    pages = query.get("pages")
    if isinstance(pages, Mapping):
        return [dict(p) for _, p in sorted(pages.items(), key=lambda kv: str(kv[0]))]
    if isinstance(pages, list):
        return [dict(p) for p in pages if isinstance(p, Mapping)]
    return []


def build_evidence_payload(
    page: Mapping[str, Any], *, fetched_at: str, api_endpoint: str | None = None
) -> dict[str, Any]:
    """保存する証拠。``fetched_at`` は ISO 8601（時計は呼び出し側が渡す）。"""
    payload: dict[str, Any] = {
        "schema": EVIDENCE_SCHEMA,
        "source": MusicSourceId.WIKIMEDIA_COMMONS.value,
        "fetched_at": fetched_at,
        "page": dict(page),
    }
    if api_endpoint is not None:
        payload["api_endpoint"] = api_endpoint
    return payload


def evidence_sha256(payload: Mapping[str, Any]) -> str:
    return sha256_hex(canonical_json_bytes(payload))


@dataclass(frozen=True, slots=True)
class CommonsFileInfo:
    facts: LicenseFacts
    title: str
    sha1: str | None
    size_bytes: int | None
    mime: str | None
    mediatype: str | None
    duration_ms: int | None


def _meta(ext: Mapping[str, Any], key: str) -> str | None:
    entry = ext.get(key)
    if not isinstance(entry, Mapping):
        return None
    value = entry.get("value")
    if value is None:
        return None
    return strip_html(str(value))


def _bool(value: str | None) -> bool | None:
    if value is None:
        return None
    lowered = value.strip().lower()
    if lowered == "true":
        return True
    if lowered == "false":
        return False
    return None


def _norm_name(title: str) -> str:
    name = title.split(":", 1)[1] if ":" in title else title
    return _SPACE.sub(" ", name.replace("_", " ")).strip().lower()


def _dispute(page: Mapping[str, Any], categories_meta: str | None) -> bool:
    names: list[str] = []
    for item in page.get("categories") or []:
        if isinstance(item, Mapping) and isinstance(item.get("title"), str):
            names.append(_norm_name(item["title"]))
    if categories_meta:
        names.extend(_norm_name(part) for part in categories_meta.split("|"))
    if any(fragment in name for name in names for fragment in _DISPUTE_FRAGMENTS):
        return True
    for item in page.get("templates") or []:
        if isinstance(item, Mapping) and isinstance(item.get("title"), str):
            title = item["title"]
            name = _norm_name(title)
            if title.startswith("Template:") and (
                name in _DISPUTE_TEMPLATES or any(f in name for f in _DISPUTE_FRAGMENTS)
            ):
                return True
    return False


def parse_commons_evidence(payload: Mapping[str, Any]) -> CommonsFileInfo:
    page = payload.get("page")
    if not isinstance(page, Mapping):
        raise ValueError("evidence payload has no page")
    title = str(page.get("title") or "")
    infos = page.get("imageinfo")
    info: Mapping[str, Any] = infos[0] if isinstance(infos, list) and infos else {}
    ext = info.get("extmetadata")
    ext = ext if isinstance(ext, Mapping) else {}
    page_url = info.get("descriptionurl")
    if not isinstance(page_url, str) or not page_url:
        page_url = COMMONS_PAGE_URL_PREFIX + title.replace(" ", "_") if title else "unknown:"
    url = info.get("url")
    categories_meta = _meta(ext, "Categories")
    duration = info.get("duration")
    facts = LicenseFacts(
        source=MusicSourceId.WIKIMEDIA_COMMONS,
        source_page_url=page_url,
        original_file_url=url if isinstance(url, str) and url else None,
        license_short_name=_meta(ext, "LicenseShortName"),
        license_url=_meta(ext, "LicenseUrl"),
        license_code=_meta(ext, "License"),
        usage_terms=_meta(ext, "UsageTerms"),
        artist=_meta(ext, "Artist"),
        credit=_meta(ext, "Credit"),
        attribution_required=_bool(_meta(ext, "AttributionRequired")),
        copyrighted=_bool(_meta(ext, "Copyrighted")),
        restrictions=_meta(ext, "Restrictions"),
        deletion_or_dispute=_dispute(page, categories_meta),
        title=_meta(ext, "ObjectName") or (title.removeprefix("File:") or None),
        evidence_sha256=evidence_sha256(payload),
    )
    size = info.get("size")
    return CommonsFileInfo(
        facts=facts,
        title=title,
        sha1=info.get("sha1") if isinstance(info.get("sha1"), str) else None,
        size_bytes=size if isinstance(size, int) else None,
        mime=info.get("mime") if isinstance(info.get("mime"), str) else None,
        mediatype=info.get("mediatype") if isinstance(info.get("mediatype"), str) else None,
        duration_ms=int(duration * 1000) if isinstance(duration, int | float) else None,
    )


__all__ = [
    "CommonsFileInfo",
    "build_evidence_payload",
    "evidence_sha256",
    "pages_from_response",
    "parse_commons_evidence",
    "strip_html",
]
