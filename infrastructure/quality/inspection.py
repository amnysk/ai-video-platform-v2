"""producer の失敗を一時的（InspectionFault）か恒久的（InspectionDefect）かに分ける（ADR-0087 §8）。

分類の表はここだけ（AGENTS.md §8）。**迷ったら恒久**（再試行しない・REVIEW のまま。PASS には
どちらでも倒れない）。再試行してよいのは「同じ入力でもう一度やれば通りうる」と言える失敗だけ。

一時（再試行する）:
- ``InspectionFault``
- ``TimeoutError`` / ``asyncio.TimeoutError`` / ``ConnectionError``（Reset・Refused・BrokenPipe 等）
- 上記以外の ``OSError``（EIO 等）。ただし下の恒久の 4 種を除く
- SQLAlchemy ``OperationalError`` / ``InterfaceError`` / pool の ``TimeoutError`` / 接続切断
- urllib3 の ``HTTPError`` 系
- MinIO ``S3Error`` のコード ``InternalError`` / ``SlowDown`` / ``ServiceUnavailable`` /
  ``RequestTimeout``

恒久（再試行しない）:
- ``InspectionDefect`` / ``ArtifactIntegrityError``（hash 不一致・別 Episode）
- ``FileNotFoundError`` / ``PermissionError`` / ``NotADirectoryError`` / ``IsADirectoryError``
- SQLAlchemy の上記以外の ``DBAPIError``（``IntegrityError`` / ``DataError`` 等）
- 上記以外のコードの ``S3Error``（``AccessDenied`` / ``NoSuchBucket`` 等）
- ``ValueError``（pydantic の ``ValidationError`` 含む）/ ``LookupError``（``KeyError``）
  / ``TypeError``
- どれでもないもの

原因を包んだ例外（``BgmEvidenceError`` 等）は ``__cause__`` の分類に従う。原因が無ければ恒久
（「別の動画の証跡」は恒久）。
"""

from __future__ import annotations

import asyncio

from sqlalchemy import exc as sa_exc

from domain.quality.inspection import FailureClass, InspectionDefect, InspectionFault

_TRANSIENT_S3_CODES = frozenset(
    {"InternalError", "SlowDown", "ServiceUnavailable", "RequestTimeout"}
)
_PERMANENT_OS = (FileNotFoundError, PermissionError, NotADirectoryError, IsADirectoryError)
_MAX_CHAIN = 5


def _is_urllib3_error(exc: BaseException) -> bool:
    return any(
        cls.__module__.startswith("urllib3") and cls.__name__ == "HTTPError"
        for cls in type(exc).__mro__
    )


def _direct(exc: BaseException) -> FailureClass | None:
    """この例外自身で決まる分類。決まらなければ ``None``（原因を辿る）。"""
    if isinstance(exc, InspectionFault):
        return FailureClass.TRANSIENT
    if isinstance(exc, InspectionDefect):
        return FailureClass.PERMANENT
    if isinstance(exc, (TimeoutError, asyncio.TimeoutError, ConnectionError)):
        return FailureClass.TRANSIENT
    if isinstance(exc, _PERMANENT_OS):
        return FailureClass.PERMANENT
    if isinstance(exc, OSError):
        return FailureClass.TRANSIENT
    if isinstance(exc, sa_exc.TimeoutError):
        return FailureClass.TRANSIENT
    if isinstance(exc, sa_exc.DBAPIError):
        transient = (
            isinstance(exc, (sa_exc.OperationalError, sa_exc.InterfaceError))
            or exc.connection_invalidated
        )
        return FailureClass.TRANSIENT if transient else FailureClass.PERMANENT
    if _is_urllib3_error(exc):
        return FailureClass.TRANSIENT
    code = getattr(exc, "code", None)
    if type(exc).__name__ == "S3Error":
        return FailureClass.TRANSIENT if code in _TRANSIENT_S3_CODES else FailureClass.PERMANENT
    if isinstance(exc, (ValueError, LookupError, TypeError)):
        return FailureClass.PERMANENT
    return None


def classify_failure(exc: BaseException) -> FailureClass:
    """``exc`` が一時的か恒久的か。総関数（どんな例外でも返す）。"""
    current: BaseException | None = exc
    for _ in range(_MAX_CHAIN):
        if current is None:
            break
        direct = _direct(current)
        if direct is not None:
            return direct
        if current.__cause__ is None:
            break
        current = current.__cause__
    return FailureClass.PERMANENT


def is_transient(exc: BaseException) -> bool:
    return classify_failure(exc) is FailureClass.TRANSIENT
