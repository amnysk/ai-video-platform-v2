"""ArtifactStore の契約（INV-9 / INV-11 / INV-17）。

実装は差し替え可能でなければならない。テストは実MinIOを使わずに
同じ契約を検査できる（INV-18）。
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol, runtime_checkable

from domain.artifact.hashing import sha256_hex
from domain.errors import ArtifactConflictError

__all__ = [
    "ArtifactConflictError",
    "ArtifactStore",
    "ObjectStat",
    "PutResult",
    "read_bytes_source",
    "readback_sha256",
]


@dataclass(frozen=True, slots=True)
class PutResult:
    key: str
    sha256: str
    size: int
    #: 既に同じ内容が存在したため書き込まなかった場合 True（再実行の証拠）。
    existed: bool


@dataclass(frozen=True, slots=True)
class ObjectStat:
    size: int
    etag: str
    content_type: str | None


def read_bytes_source(data: bytes | Path) -> bytes:
    """``put_bytes`` の入力をバイト列にする（メディアは上限 25MB なのでメモリに載せてよい）。"""
    return data if isinstance(data, bytes) else Path(data).read_bytes()


async def readback_sha256(store: ArtifactStore, key: str) -> str:
    """保存物を読み戻して sha256 を取る。一致しない保存物を「現行」にしないための検査。"""
    return sha256_hex(await store.get_bytes(key))


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

    async def put_bytes(self, key: str, data: bytes | Path, content_type: str) -> PutResult:
        """バイナリ（メディア本体）を保存する（ADR-0017）。immutability は ``put_json`` と同じ。"""
        ...

    async def get_bytes(self, key: str) -> bytes:
        """無ければ ``KeyError``。"""
        ...

    async def stat(self, key: str) -> ObjectStat:
        """無ければ ``KeyError``。"""
        ...

    async def exists(self, key: str) -> bool: ...
