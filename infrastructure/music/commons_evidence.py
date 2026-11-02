"""Commons の証拠の sha256 と解析の結線（domain 側は hash 実装を持たない）。"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from domain.artifact.hashing import canonical_json_bytes, sha256_hex
from domain.music.commons_facts import CommonsFileInfo, parse_commons_evidence


def evidence_sha256(payload: Mapping[str, Any]) -> str:
    """証拠 payload の正準 JSON の sha256。"""
    return sha256_hex(canonical_json_bytes(payload))


def parse_evidence(payload: Mapping[str, Any]) -> CommonsFileInfo:
    return parse_commons_evidence(payload, evidence_sha256=evidence_sha256(payload))


__all__ = ["evidence_sha256", "parse_evidence"]
