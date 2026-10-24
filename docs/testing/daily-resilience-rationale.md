# テスト設計の根拠: 日次の事前診断・保留・watchdog 分類（ADR-0058）

背景: 2026-10 の日次 7 日連続失敗（Codex の model 不一致）と、fal の 403（残高 / 鍵が未確定）。
どのテストも本物の codex・fal・YouTube を呼ばない（AGENTS.md §9）。

## Codex model の事前診断（INV-75）

| テスト | 止めるもの |
|---|---|
| `tests/unit/test_codex_model_preflight.py::test_model_missing_from_the_catalog_blocks_before_any_prompt` | catalog に無い model（gpt-5.5 型）が、実行して 400 を受けるまで分からない退行 |
| `::test_a_catalogued_model_is_never_reported_as_verified_without_an_allowlist` | gpt-6-sol 型（catalog にあるのに 400）を `OK` と主張する退行。`OK` は許可リストにもあるときだけ |
| `::test_a_model_outside_the_allowlist_blocks` | 許可リスト（実測した集合）外の model が日次を通る |
| `::test_a_model_in_both_allowlist_and_catalog_is_ok` / `::test_allowlist_member_missing_from_catalog_still_blocks` | 許可リストだけを信じて catalog との食い違いを見逃す |
| `::test_unpinned_model_from_the_shared_config_is_a_warning_until_pinning_is_required` | 共有 config.toml 由来の model が、固定必須の運用でも通る |
| `::test_unreadable_or_stale_catalog_never_blocks_and_never_verifies` | 読めない・古い catalog を根拠に日次を止める（誤停止）/ 確認できたことにする |
| `::test_read_models_cache_parses_slugs_and_fetch_time` | codex の `fetched_at`（9 桁小数秒・Z）を読めず catalog が常に「古い」扱いになる |
| `::test_run_preflight_reads_files_only` | 設定ファイル以外（prompt・ネットワーク）に出ない |
| `tests/contract/test_codex_env_single_source.py` | 3 つの Codex worker の model 設定が別々に書かれて食い違う（AGENTS §8） |
