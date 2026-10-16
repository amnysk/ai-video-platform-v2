"""Topic 文字列の長さ上限（単一の宣言元）。

Topic は TopicCandidate / ScriptArtifact / API / ``episodes.topic`` 列を通る。
上限はここだけで決め、各所はこの定数を参照する（DB 列は migration 0009 で揃える）。
"""

from __future__ import annotations

TOPIC_MAX_CHARS = 200
