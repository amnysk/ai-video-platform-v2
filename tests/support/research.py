"""Research の依頼 payload の組み立て（テスト専用。ADR-0037）。"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

AS_OF = datetime(2026, 9, 29, 3, 0, tzinfo=UTC)


def _base(kind: str) -> dict[str, Any]:
    return {
        "kind": kind,
        "requester": "script",
        "channel_id": "channel-1",
        "audience": "history fans",
        "language": "en",
        "format_profile": "shorts",
        "time_window": {
            "start": (AS_OF - timedelta(days=7)).isoformat(),
            "end": AS_OF.isoformat(),
        },
        "as_of": AS_OF.isoformat(),
    }


def trend_payload(**overrides: Any) -> dict[str, Any]:
    payload = _base("trend")
    payload["inputs"] = {"region": "US", "audience_hypothesis": "curious adults"}
    payload.update(overrides)
    return payload


def evidence_payload(**overrides: Any) -> dict[str, Any]:
    payload = _base("evidence")
    payload["inputs"] = {
        "claim_inputs": [
            {"claim_text": "The bridge opened in 1883", "kind": "year", "importance": "central"}
        ]
    }
    payload.update(overrides)
    return payload
