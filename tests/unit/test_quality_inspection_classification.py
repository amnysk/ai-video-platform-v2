"""producer の失敗の分類表（ADR-0087 §8 / INV-156）。表の 1 行 = 1 ケース。"""

from __future__ import annotations

import pytest
from pydantic import BaseModel, ValidationError
from sqlalchemy import exc as sa_exc

from domain.quality.inspection import FailureClass, InspectionDefect, InspectionFault
from infrastructure.music.evidence import BgmEvidenceError
from infrastructure.quality.inspection import classify_failure
from infrastructure.quality.service import ArtifactIntegrityError

T, P = FailureClass.TRANSIENT, FailureClass.PERMANENT


class _S3Error(Exception):
    """minio.error.S3Error の代役（クラス名と code だけを見る）。"""

    def __init__(self, code: str) -> None:
        self.code = code


_S3Error.__name__ = "S3Error"


class _Url3HTTPError(Exception):
    pass


_Url3HTTPError.__module__ = "urllib3.exceptions"
_Url3HTTPError.__name__ = "HTTPError"


class _MaxRetry(_Url3HTTPError):
    pass


def _validation_error() -> ValidationError:
    class M(BaseModel):
        x: int

    try:
        M(x="not a number")  # type: ignore[arg-type]
    except ValidationError as err:
        return err
    raise AssertionError


def _wrapped(cause: BaseException) -> BgmEvidenceError:
    try:
        raise BgmEvidenceError("bgm evidence unreadable") from cause
    except BgmEvidenceError as err:
        return err


class _DbApi(Exception):
    pass


CASES: list[tuple[str, BaseException, FailureClass]] = [
    ("explicit fault", InspectionFault("x"), T),
    ("explicit defect", InspectionDefect("x"), P),
    ("TimeoutError", TimeoutError(), T),
    ("asyncio.TimeoutError", TimeoutError(), T),
    ("ConnectionResetError", ConnectionResetError(), T),
    ("ConnectionRefusedError", ConnectionRefusedError(), T),
    ("BrokenPipeError", BrokenPipeError(), T),
    ("generic OSError (EIO)", OSError(5, "io error"), T),
    ("FileNotFoundError", FileNotFoundError(), P),
    ("PermissionError", PermissionError(), P),
    ("IsADirectoryError", IsADirectoryError(), P),
    ("sqlalchemy OperationalError", sa_exc.OperationalError("s", {}, Exception("db down")), T),
    ("sqlalchemy InterfaceError", sa_exc.InterfaceError("s", {}, Exception("x")), T),
    ("sqlalchemy pool timeout", sa_exc.TimeoutError("pool"), T),
    ("sqlalchemy IntegrityError", sa_exc.IntegrityError("s", {}, Exception("dup")), P),
    ("sqlalchemy DataError", sa_exc.DataError("s", {}, Exception("x")), P),
    ("sqlalchemy ProgrammingError", sa_exc.ProgrammingError("s", {}, Exception("x")), P),
    ("urllib3 HTTPError family", _MaxRetry(), T),
    ("S3 InternalError", _S3Error("InternalError"), T),
    ("S3 SlowDown", _S3Error("SlowDown"), T),
    ("S3 ServiceUnavailable", _S3Error("ServiceUnavailable"), T),
    ("S3 AccessDenied", _S3Error("AccessDenied"), P),
    ("S3 NoSuchBucket", _S3Error("NoSuchBucket"), P),
    ("ArtifactIntegrityError", ArtifactIntegrityError("hash mismatch"), P),
    ("pydantic ValidationError", _validation_error(), P),
    ("ValueError", ValueError("bad contract"), P),
    ("KeyError (object absent)", KeyError("k"), P),
    ("TypeError", TypeError("x"), P),
    ("BgmEvidenceError belongs to another video", BgmEvidenceError("another video"), P),
    ("BgmEvidenceError from OSError", _wrapped(OSError(5, "io")), T),
    ("BgmEvidenceError from ConnectionError", _wrapped(ConnectionResetError()), T),
    ("BgmEvidenceError from ValueError", _wrapped(ValueError("x")), P),
    ("unknown RuntimeError", RuntimeError("???"), P),
    ("unknown Exception", Exception("???"), P),
]


@pytest.mark.parametrize(("name", "exc", "expected"), CASES, ids=[c[0] for c in CASES])
def test_classification_table(name: str, exc: BaseException, expected: FailureClass) -> None:
    assert classify_failure(exc) is expected


def test_cause_chain_is_bounded_and_total() -> None:
    err: BaseException = RuntimeError("root")
    for _ in range(50):
        nxt = RuntimeError("wrap")
        nxt.__cause__ = err
        err = nxt
    assert classify_failure(err) is P  # 例外を投げず、迷えば恒久
    loop = RuntimeError("a")
    loop.__cause__ = loop
    assert classify_failure(loop) is P
