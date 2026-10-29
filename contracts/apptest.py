"""隔離 app スタック（run-e2e.sh）の project 名の規則（唯一の宣言元。AGENTS.md §8）。

本番 project ``avp2`` は含まない。run-e2e.sh の ``case`` は同じ接頭辞を書いており、
一致は tests/contract/test_apptest_project_pattern.py が検査する。
"""

from __future__ import annotations

import re

APPTEST_PROJECT_PREFIX = "avp2-oslog-c"
#: 接頭辞そのもの、または接頭辞 + ``[-_0-9a-z]*``
APPTEST_PROJECT_RE = re.compile(re.escape(APPTEST_PROJECT_PREFIX) + r"(?:[-_0-9a-z]*)")
