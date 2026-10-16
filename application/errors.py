"""操作のエラー。定義は ``contracts.application``（infrastructure の port も投げるため）。"""

from __future__ import annotations

from contracts.application import OperationError, OperationErrorCode

__all__ = ["OperationError", "OperationErrorCode"]
