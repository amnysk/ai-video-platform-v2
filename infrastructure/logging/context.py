"""ログの文脈（contextvars。log-contract §5）。

``with log_context(episode_id=..., scene_id=...):`` の中で出た記録は、整形器がこの値を付ける。
抜けるときに token で必ず元へ戻す（例外・cancel でも）。Activity ごと・API リクエストごとに
別 task なので、並列の Episode 間で混ざらない。Workflow は使わない（Workflow から見えない）。
"""

from __future__ import annotations

import contextvars
from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from types import MappingProxyType
from typing import Any

_EMPTY: Mapping[str, Any] = MappingProxyType({})
_CONTEXT: contextvars.ContextVar[Mapping[str, Any]] = contextvars.ContextVar(
    "avp_log_context", default=_EMPTY
)


def current_context() -> Mapping[str, Any]:
    return _CONTEXT.get()


@contextmanager
def log_context(**fields: Any) -> Iterator[None]:
    """``None`` の値は束縛しない（未取得の値を付けない / log-contract §2）。"""
    merged = {**_CONTEXT.get(), **{k: v for k, v in fields.items() if v is not None}}
    token = _CONTEXT.set(MappingProxyType(merged))
    try:
        yield
    finally:
        _CONTEXT.reset(token)


__all__ = ["current_context", "log_context"]
