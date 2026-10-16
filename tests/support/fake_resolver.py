"""DNS の Fake（research の SSRF 検査用）。実 DNS には出ない。"""

from __future__ import annotations

from collections.abc import Mapping, Sequence

from infrastructure.research.errors import ResolutionError


class FakeResolver:
    """host → 解決結果。``rebinding`` で「呼ぶたびに答えが変わる」DNS rebinding を再現する。"""

    def __init__(self, table: Mapping[str, Sequence[str]] | None = None) -> None:
        self._answers: dict[str, list[list[str]]] = {
            host.lower(): [list(addrs)] for host, addrs in (table or {}).items()
        }
        self.calls: list[tuple[str, int]] = []

    def rebinding(self, host: str, *answers: Sequence[str]) -> FakeResolver:
        """1 回目は ``answers[0]``、2 回目は ``answers[1]``…（最後の答えを繰り返す）。"""
        self._answers[host.lower()] = [list(a) for a in answers]
        return self

    async def resolve(self, host: str, port: int) -> list[str]:
        self.calls.append((host, port))
        sequence = self._answers.get(host.lower())
        if sequence is None:
            raise ResolutionError(f"NXDOMAIN: {host}")
        index = min(sum(1 for h, _ in self.calls if h == host) - 1, len(sequence) - 1)
        return list(sequence[index])
