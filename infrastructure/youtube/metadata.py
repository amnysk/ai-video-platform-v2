"""契約のメタデータを ``videos.insert`` の JSON body へ写す（ADR-0020 §5）。

API の欄名（camelCase）を知ってよいのは YouTube adapter だけ。``notifySubscribers`` は query
parameter なので body に入れない（uploader が常に ``false`` を送る）。
"""

from __future__ import annotations

from typing import Any

from contracts.upload import YouTubeVideoMetadata


def insert_body(metadata: YouTubeVideoMetadata) -> dict[str, Any]:
    snippet: dict[str, Any] = {
        "title": metadata.title,
        "description": metadata.description,
        "tags": list(metadata.tags),
        "categoryId": metadata.category_id,
    }
    if metadata.default_language is not None:
        snippet["defaultLanguage"] = metadata.default_language
    return {
        "snippet": snippet,
        "status": {
            # INV-19: 契約が Literal["private"] なのでここも private 以外にならない
            "privacyStatus": metadata.privacy_status,
            "selfDeclaredMadeForKids": metadata.self_declared_made_for_kids,
            "containsSyntheticMedia": metadata.contains_synthetic_media,
        },
    }


__all__ = ["insert_body"]
