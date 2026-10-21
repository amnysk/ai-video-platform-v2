# テスト設計の根拠: Codex model ガード

| テスト | 止めるもの |
|---|---|
| `tests/unit/test_codex_model_guard.py::test_extract_unsupported_model_*` | 400 本文から model 名を取れなくなる退行（原因が「failed:」だけになる） |
| `tests/unit/test_codex_adapter.py::test_rejection_carries_the_model_name_and_is_not_retryable` | config.toml 由来（`-m` 無し）でも model 名が出ること、`needs_input` で retry 予算を使わないこと |
| `tests/unit/test_codex_adapter.py::test_rejection_message_has_no_secret` | エラー文面への secret 混入（AGENTS.md §9） |
| `test_configured_model_reads_only_the_top_level_key` | `[projects.*]` 配下の `model` を誤読しないこと、壊れた toml で起動検査が落ちないこと |
| `test_effective_model_prefers_the_explicit_setting` | `CODEX_MODEL` > config.toml の優先順位（codex の `-m` 挙動と一致） |
| `test_verify_without_allowlist_never_claims_the_model_works` | 許可リスト未設定時に「OK」と主張しない（既定は未検証） |
| `test_verify_rejects_a_model_outside_the_allowlist` / `..._accepts_...` | リスト外の fail-fast とリスト内の通過 |

背景: 2026-10-14〜20 の日次 7 日連続失敗（docs/operations/codex-model-config.md）。
