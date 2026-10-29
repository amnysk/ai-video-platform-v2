"""staging の識別子（唯一の宣言元。AGENTS.md §8）。

``compose.staging.yaml`` の値と release preflight の「本番ではない対象を指していないか」の判定が
同じ値を使う。一致は tests/contract/test_compose_staging.py が検査する。
"""

from __future__ import annotations

from contracts.quality import QUALITY_GATE_ENFORCE_ENV
from contracts.upload import UPLOAD_DESCRIPTION_COMPOSITION_ENV

STAGING_TEMPORAL_NAMESPACE = "avp-staging"
STAGING_DB_NAME = "avp_staging"
STAGING_ENVIRONMENT_MARK = "staging"
#: 破壊的テストだけが向く DB 名の接尾辞（ADR-0021）。本番 preflight の対象ではない
TEST_DB_SUFFIX = "_test"


#: 「新機能を ON にする」にあたる設定キー（ADR-0080 §A）。**唯一の宣言元**。
#: release preflight が対象環境の実際の値と承認記録（委任か本人か）を突き合わせる。
#: 追加はあっても外さない。各キーが既定 OFF であることは
#: tests/unit/test_release_feature_flags.py が検査する
FEATURE_FLAG_ENV_KEYS: tuple[str, ...] = (
    QUALITY_GATE_ENFORCE_ENV,
    "BGM_ENABLED",
    "GEO_MAPS_ENABLED",
    UPLOAD_DESCRIPTION_COMPOSITION_ENV,
)
