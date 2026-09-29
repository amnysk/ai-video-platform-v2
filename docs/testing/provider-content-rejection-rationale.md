# テスト設計の根拠: provider のコンテンツ拒否（ADR-0034）

各テストが**なぜテストになったか**を残す。事故は 2026-09-26・09-27 の2日連続、Episode
`dedcf315-256f-4769-9baf-6d4bb6bf6dfa`（sb4）と `ec44fadd-3de8-44af-9060-ad538c93d103`
（sb2）が fal の `HTTP 422 content_policy_violation` で `blocked` になったもの。原因分析と
決定は `docs/decisions/0034-provider-content-rejection-retry-and-prompt-mitigation.md`。

## `tests/unit/test_fal_queue.py`

| テスト | なぜ必要か / 落ちたら何が起きているか |
|---|---|
| `test_content_policy_rejection_keeps_the_full_reason_without_cutting_mid_word` | **事故の直接再現**。実際に fal が返した形（`detail` が pydantic validation error のリスト）を使い、`msg`（拒否理由の文）・`ctx.extra_info.reason`・`loc` が省略されずに残ることを検査する。旧実装（`str(body["detail"])[:500]`）は Python の repr 表現ごと単語の途中で切っていた（実際に584文字で切れて理由が読めなかった）。このテストが落ちれば、運用者がまた拒否理由を読めなくなっている |

`test_result_failures_are_classified`（既存）は 422 `content_policy_violation` が
`ProviderRejectedError` に分類されることを既にこの起点で検査していた。分類自体は
変更していないので、この既存テストは変更していない。

## `tests/unit/test_paid_job.py`

| テスト | なぜ必要か / 落ちたら何が起きているか |
|---|---|
| `test_provider_rejected_input_blocks_the_next_round` | **事故の核**。provider が入力を拒否して `spent` した予約に対し、次の `submit()`（= Episode の resume）が新しいラウンド・新しい予約・新しい provider 呼び出しを作らないこと。`ProviderRejectedRetryBlockedError` を送出し、台帳に新しい行が増えないこと（`_rounds` で検査）・`generator.submit` が呼ばれないこと（`gen.submit_calls == 1` のまま）を確認する。落ちれば、resume のたびに同じ拒否を繰り返し課金する経路が復活している |
| `test_provider_rejected_input_does_not_block_a_different_input_hash` | 上のブロックが**恒久停止にならない**こと。プロンプト・素材を直して `input_hash` が変われば、通常どおりラウンド1として新しい提出ができる。落ちれば、プロンプトを直しても永久に動かない（過剰に安全側へ倒れている）バグが入っている |

`test_job_failure_spends_conservatively`（既存、`JobFailed(..., rejected=True)` →
`ProviderRejectedError` のケースを含む）と `test_clean_not_accepted_submit_is_conservatively_spent`
（既存、`ProviderJobFailedError` で次ラウンドへ進む）はどちらも変更していない。前者は
次ラウンドの submit を試みない（今回のテストと重複しない）。後者は `ProviderRejectedError`
ではないので `input_rejected_by_provider` は立たず、これまでどおり次ラウンドへ進む
（今回のゲートの対象外であることの回帰検査を兼ねる）。

## `tests/unit/test_failure_class_registry.py`

| テスト | なぜ必要か |
|---|---|
| `test_production_exceptions_classify_by_their_base`（`ProviderRejectedRetryBlockedError` を追加） | 新しい例外が `needs_input` に分類され、型名テーブル（`FAILURE_CLASS_BY_TYPE_NAME`）経由でも Temporal の retry を止める（`NON_RETRYABLE_ERROR_TYPE_NAMES`）ことを固定する。`test_every_domain_exception_is_reachable_by_type_name`（既存、変更なし）は継承ベースの自動導出なので、新しい例外を手で登録し忘れても検出する |

## `tests/unit/test_fal_seedream_image.py` / `tests/unit/test_fal_seedance_video.py`

| テスト | なぜ必要か |
|---|---|
| `test_prompt_avoids_asking_for_a_photorealistic_likeness_of_a_real_person`（新規） | ADR-0034 のプロンプト緩和策（写実的な肖像ではなく様式化した挿絵）が実際にプロンプト文面へ入ることを固定する |
| `test_style_profile_id_carries_the_builder_version` / `test_video_prompt_builder`（既存、版番号のみ更新） | `IMAGE_PROMPT_BUILDER_VERSION` / `VIDEO_PROMPT_BUILDER_VERSION` を `1` → `2` に上げた（文面を変えたら版を上げる規約）。版を上げないと、古い画風の画像が新しい規則の結果として誤って再利用される。既存テストの変更理由はこの版上げそのもの（ADR-0034、意図的な仕様変更） |
