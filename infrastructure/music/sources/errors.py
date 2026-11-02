"""音源ソースの型付きエラー。収集は種類で扱いを分ける（再試行できるか・その候補を捨てるか）。"""

from __future__ import annotations


class SourceUnavailableError(Exception):
    """一時的な障害（タイムアウト・5xx・429・maxlag・接続）。後で再試行してよい。"""


class CandidateInvalidError(Exception):
    """この候補は使えない（拡張子・サイズ・ホスト・sha1 の不一致）。再試行しても同じ。"""

    def __init__(self, reason: str, detail: str = "") -> None:
        super().__init__(f"{reason}: {detail}" if detail else reason)
        self.reason = reason


__all__ = ["CandidateInvalidError", "SourceUnavailableError"]
