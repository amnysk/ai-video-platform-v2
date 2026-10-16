"""``domain.production.ports.MediaDestination`` のファイル実装（ADR-0017）。"""

from __future__ import annotations

from pathlib import Path


class FileMediaDestination:
    """作業領域内のファイルへ書く。``path`` を公開するので、子プロセスに直接書かせてもよい。"""

    def __init__(self, path: Path) -> None:
        self.path = path

    async def write(self, chunk: bytes) -> None:
        with self.path.open("ab") as handle:
            handle.write(chunk)


__all__ = ["FileMediaDestination"]
