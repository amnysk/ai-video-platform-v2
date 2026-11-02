"""staging の識別子（唯一の宣言元。AGENTS.md §8）。

``compose.staging.yaml`` の値と release preflight の「本番ではない対象を指していないか」の判定が
同じ値を使う。一致は tests/contract/test_compose_staging.py が検査する。
"""

from __future__ import annotations

from contracts.music_sources import BGM_ACQUISITION_SETTING_KEY
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
#: BGM の自動収集（ADR-0087 §10 / INV-159）。キー名は contracts.music_sources が唯一の定義
BGM_ACQUISITION_ENV = BGM_ACQUISITION_SETTING_KEY.upper()

FEATURE_FLAG_ENV_KEYS: tuple[str, ...] = (
    QUALITY_GATE_ENFORCE_ENV,
    "BGM_ENABLED",
    "GEO_MAPS_ENABLED",
    UPLOAD_DESCRIPTION_COMPOSITION_ENV,
    BGM_ACQUISITION_ENV,
)

#: 各 flag を読む compose service（R3。**唯一の宣言元**）。``compose.yaml`` はここに挙げた
#: service にだけ ``FLAG: ${FLAG:-false}`` で渡す。読み手が増えたら先にここへ足す
#: （tests/contract/test_compose_feature_flags.py が compose と entrypoint を突き合わせる）。
#: ENFORCE: api（手動 upload の門）/ pipeline（自動 upload の門）/ render（使用宣言の書き込み）/
#: production（地図 ON のときの起動ガード）/ upload（投稿 Activity 内の最後の門と BGM の起動ガード。
#: INV-158）。
#: BGM: render（描画）/ upload（起動ガード・credits）/ bgm-acquisition-worker（収集の起動ガード）。
#: BGM_ACQUISITION: bgm-acquisition-worker だけ（profile `bgm-acquisition`。既定では起動しない）。
#: GEO: render / production。COMPOSED: 説明欄を組む api・pipeline（品質 lint）・upload と、
#: render の起動ガード
FEATURE_FLAG_SERVICES: dict[str, tuple[str, ...]] = {
    QUALITY_GATE_ENFORCE_ENV: (
        "api",
        "pipeline-worker",
        "render-worker",
        "production-worker",
        "upload-worker",
        "bgm-acquisition-worker",
    ),
    "BGM_ENABLED": ("render-worker", "upload-worker", "bgm-acquisition-worker"),
    BGM_ACQUISITION_ENV: ("bgm-acquisition-worker",),
    "GEO_MAPS_ENABLED": ("render-worker", "production-worker"),
    UPLOAD_DESCRIPTION_COMPOSITION_ENV: (
        "api",
        "pipeline-worker",
        "render-worker",
        "upload-worker",
    ),
}
