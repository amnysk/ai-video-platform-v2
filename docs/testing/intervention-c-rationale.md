# 介入 API・CLI（JP-MAS-001 担当 C）のテストの理由

設計 [JP-MAS-001 v2.0](../design/JP-MAS-001-v2.0.md) §10、契約 [docs/domain/intervention.md](../domain/intervention.md) §4/§8、
[ADR-0041](../decisions/0041-decision-records-and-intervention.md) §8、不変条件 INV-47。HTTP の形は
[docs/operations/intervention-api.md](../operations/intervention-api.md)。

守る事故は2系統:

1. **既定で開く**: API は `0.0.0.0:8000` に bind しており、認証が無い（ADR-0041 Context の実査）。介入 API を足した
   だけで、ネットワーク上の誰でも hold・適用・mode 変更ができる状態を作ってしまう。
2. **AI が人の権限を持つ**: AI アシスタント・agent が mode を変える、予算・Schedule・公開設定に触れる（設計 §10、INV-47）。

全て unit（FastAPI のアプリ・設定の解析・CLI を fake と SQLite で動かす）。実 Temporal・実 MinIO・有料 API には
到達しない（INV-18）。B・A の実モジュールとの接続（`apps/api/intervention_backend.py::load_real_modules`）は
統合後に親が検査する（この文書の末尾）。

## principal 設定（`tests/unit/test_intervention_auth.py`）

| テスト | 何の失敗を捕まえるか |
|---|---|
| `test_token_hash_is_plain_sha256_hex` | 運用手順（`scripts/intervene.py hash-token` / `sha256sum`）で作った hash と照合方法がずれる |
| `test_authenticate_maps_a_token_to_its_principal` | token → actor・scope の対応が崩れる（別人として記録される） |
| `test_unknown_or_empty_token_is_not_authenticated` | 空 token・**hash そのもの**を token として出すと通る（設定ファイルが漏れたら即なりすまし） |
| `test_ai_principals_cannot_be_granted_human_only_scopes` | INV-47: AI（`ai_assistant` / `agent`）に `set_mode` を与えた設定が読み込めてしまう。`HUMAN_ONLY_SCOPES` から引くので、契約に人専用 scope が増えたら自動で対象になる |
| `test_system_actor_cannot_be_an_api_principal` | `system`（worker・Schedule の主体）を名乗る外部 principal で、人・AI の区別が記録から消える |
| `test_malformed_configuration_is_rejected` | 平文 token を key に置いた・大文字 hex・未知 scope（`set_budget` 等）・余分な項目・空 actor_id を黙って受け入れる |
| `test_config_errors_never_echo_the_key_or_a_clear_token` | 設定の誤りの例外文（ログに出る）に平文 token・hash が載る |
| `test_load_returns_none_when_unconfigured` / `test_settings_default_is_unconfigured` | 既定で介入 API が有効になる（事故 1） |
| `test_load_accepts_inline_json_or_a_file_path` / `test_load_rejects_a_missing_file` | 運用で使う2形式のどちらかが読めない・読めないファイルを「未設定」として黙って扱う |
| `test_settings_hide_the_principal_setting_in_dumps` | 設定値が `model_dump` / repr に出る（`SecretStr`） |
| `test_parsing_does_not_log_secrets` | 解析の経路が token・hash をログに書く |

unit で十分な理由: 解析は純粋関数で、外部 I/O はファイル読み込み1回だけ。
