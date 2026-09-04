"""ArtifactStore の契約（INV-9 / INV-11 / INV-17）。

実装は差し替え可能でなければならない。テストは実MinIOを使わずに
同じ契約を検査できる（INV-18）。
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any, Protocol, runtime_checkable

from domain.errors import ArtifactConflictError

__all__ = ["ArtifactConflictError", "ArtifactStore", "PutResult"]


@dataclass(frozen=True, slots=True)
class PutResult:
    key: str
    sha256: str
    size: int
    #: 既に同じ内容が存在したため書き込まなかった場合 True（再実行の証拠）。
    existed: bool


@runtime_checkable
class ArtifactStore(Protocol):
    async def put_json(self, key: str, payload: Mapping[str, Any]) -> PutResult:
        """正準JSONとして保存する。

        同じキーに**同じ内容**なら書き込まずに ``existed=True`` を返す（INV-17）。
        同じキーに**異なる内容**なら ``ArtifactConflictError``（INV-11）。
        """
        ...

    async def get_json(self, key: str) -> dict[str, Any]: ...

    async def put_text(self, key: str, body: str) -> PutResult:
        """検証を通らない生テキスト（provider の生出力）を保存する（ADR-0013）。

        これは **Artifact ではない**。スキーマを持たないので
        ``artifact_metadata`` には載せず、予約台帳の ``raw_output_key`` からのみ
        参照する。immutability（INV-11）は Artifact と同じく適用する
        ── 「呼んだ証拠」を書き換えられては照合の意味が無い。
        """
        ...

    async def get_text(self, key: str) -> str: ...

    async def exists(self, key: str) -> bool: ...
