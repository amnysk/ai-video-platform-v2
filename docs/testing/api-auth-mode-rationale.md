# API 認証の明示的な無効化（ADR-0080 追補 2026-11-01）: テストを置いた理由

各テストが**何の失敗を防ぐか**を残す。INV-124 追補。

## tests/unit/test_api_auth_mode.py

| テスト | 防ぐ失敗 |
|---|---|
| `test_the_default_is_required` | 設定を書かない環境で認証が外れる |
| `test_only_an_explicit_disabled_turns_authentication_off` | `disabled` と書いても効かない / 大文字・空白で食い違う |
| `test_any_other_value_stays_required_and_does_not_stop_the_boot` | 綴り違い（`disable`, `off`）で認証が外れる、または検証エラーで API が起動しない |
| `test_the_setting_reads_the_documented_environment_key` | compose / .env のキー名と Settings の読むキーがずれる |
| `test_disabled_lets_a_mutation_through_without_reading_any_token_file` | disabled でも 503 のまま（deploy 後に resume 等が止まる）/ token ファイルを読む / 「認証無効」のログが出ない |
| `test_disabled_ignores_a_bad_credential_and_never_logs_it` | 送られた credential がログに出る |
| `test_required_with_no_tokens_is_still_503` | opt-out の追加で、既定・未知値の fail closed（INV-124）が壊れる |
| `test_required_with_tokens_still_demands_a_valid_credential` | required の 401 / actor 付与が変わる |
| `test_reads_never_log_the_disabled_notice` | GET のたびにログが出る（ノイズ・読み取りは開けたまま） |
| `test_an_unauthenticated_request_never_becomes_an_approver` | 無認証のリクエストが REVIEW を解放する承認者になる（INV-125） |
| `test_compose_passes_the_mode_to_the_api_with_a_required_default` | compose が値を渡さず .env の設定が API に届かない / 既定が required でない |

## tests/unit/test_release_api_token_check.py / test_release_collectors_flags_and_tokens.py

| テスト | 防ぐ失敗 |
|---|---|
| `test_an_owner_disabled_auth_passes_without_a_token_file` | 所有者が無認証と決めたのに preflight が token ファイル無しで BLOCKED のまま |
| `test_anything_but_an_explicit_disabled_keeps_the_token_check` | disabled 以外で token チェックが緩む |
| `test_the_probe_reads_the_auth_mode_from_the_dotenv` | preflight が deploy が適用する値（.env）と別の値を見る |
| `test_an_auth_mode_in_the_process_environment_that_differs_from_the_dotenv_is_unknown` | .env とプロセス環境が食い違うのに PASS / BLOCKED と断定する |
| `test_a_matching_process_environment_is_accepted` | 一致しているのに UNKNOWN になり続ける |
