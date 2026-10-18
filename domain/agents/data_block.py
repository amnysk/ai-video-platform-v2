"""外部由来の値を prompt に入れるときの ``<data>`` 区切り（ADR-0043 §6、ADR-0047 INV-60）。

handler の prompt と Agent Tool の結果（``render_untrusted``）が使う区切りの唯一の定義。
I/O を持たない。
"""

from __future__ import annotations

import json
from typing import Any


def data_block(name: str, value: Any) -> str:
    # "<" をエスケープして、DATA が区切りを閉じて指示を装えないようにする（JSON として有効なまま）
    text = json.dumps(value, ensure_ascii=False, sort_keys=True, default=str)
    return f'<data name="{name}">\n{text.replace("<", "\\u003c")}\n</data>'
