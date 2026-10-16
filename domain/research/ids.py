"""Research の識別子（ADR-0037 §2）。純粋関数のみ。

``RESEARCH_NAMESPACE`` はここが**唯一の宣言元**。値を変えると既存の ``request_id`` の導出が
変わり、同じ冪等キーの再送が別 ID を作るので、変更しない。
"""

from __future__ import annotations

import uuid

#: ``request_id = uuid5(RESEARCH_NAMESPACE, idempotency_key)``。固定の乱数 UUID（変更禁止）。
RESEARCH_NAMESPACE = uuid.UUID("3f6a1c52-9d0b-4b7e-8a54-6c1e2f7d9b30")


def research_request_id_for(idempotency_key: str) -> str:
    """冪等キーから決定的に request_id を導く（再送が別 ID を作らない）。"""
    if not idempotency_key:
        raise ValueError("idempotency_key must be non-empty")
    return str(uuid.uuid5(RESEARCH_NAMESPACE, idempotency_key))


__all__ = ["RESEARCH_NAMESPACE", "research_request_id_for"]
