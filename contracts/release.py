"""staging の識別子（唯一の宣言元。AGENTS.md §8）。

``compose.staging.yaml`` の値と release preflight の「本番ではない対象を指していないか」の判定が
同じ値を使う。一致は tests/contract/test_compose_staging.py が検査する。
"""

from __future__ import annotations

STAGING_TEMPORAL_NAMESPACE = "avp-staging"
STAGING_DB_NAME = "avp_staging"
STAGING_ENVIRONMENT_MARK = "staging"
#: 破壊的テストだけが向く DB 名の接尾辞（ADR-0021）。本番 preflight の対象ではない
TEST_DB_SUFFIX = "_test"
