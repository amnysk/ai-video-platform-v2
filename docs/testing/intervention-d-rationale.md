# 判断記録と介入: 既存接続・ログ・試験（担当 D）のテストの理由

JP-MAS-001 v2.0 / ADR-0041。各テストが**何の事故・契約**を守り、**なぜその層**で書き、**何の失敗を捕まえる**かを
記録する（AGENTS.md・memory「test-design-rationale」）。fake / stub を使うものはそう明記する。

## 1. ログ語彙（`tests/contract/test_intervention_log_vocabulary.py`）

| テスト | 守る契約・事故 | 層の理由 | 捕まえる失敗 |
|---|---|---|---|
| `test_every_intervention_event_is_a_log_event_name` | ADR-0041 §7: `episode_events.event_name` とログの `event_name` は同じ文字列 | 2つの enum の集合比較だけで決まる。実行は要らない | `InterventionEvent` にだけ足して投影がログ語彙に無い値を出す（AGENTS.md §8 の片側更新） |
| `test_intervention_event_members_have_the_same_python_names` | 投影の対応表をメンバー名で引ける | 同上 | 文字列は同じでもメンバー名がずれ、対応表の網羅検査が通らない |
| `test_correlation_fields_of_the_design_are_searchable_fields` | 設計 §15 の相関項目・agent 共通項目が検索できる型で mapping にある（既存へ写したものは写し先の型） | mapping は契約から生成されるので、契約の型を見れば足りる | 項目を `attributes`（検索対象外）にしか置かず、監視（§15）で絞り込めない |
| `test_new_field_names_do_not_look_like_secrets` | 件数・費用のフィールドが安全化の鍵名規則（§7.2 の `token` 部分一致）で伏せられない | 規則の関数を直接呼ぶ | `token_usage` という名前にして、`attributes` に落ちた時や文中の `key=value` で値が `[REDACTED]` になる |
| `test_actor_types_are_documented` | `actor_type` の語彙が log-contract.md にある（AGENTS.md §7） | 文書の文字列検査 | 語彙を足して文書を更新し忘れる |

## 2. 投影と再投影（`tests/unit/test_intervention_projection.py`、SQLite・実の JSON 整形器）

正本（`episode_events`）と投影（stdout の JSON）の境界を守る。OpenSearch は使わない（アプリは OpenSearch を
読まない・INV-38。Collector から先は隔離環境の試験 = 担当 D の phase 2）。

| テスト | 守る契約・事故 | 層の理由 | 捕まえる失敗 |
|---|---|---|---|
| `test_every_intervention_event_has_an_explicit_log_event` | 対応表が網羅的で同じ文字列 | 表の比較だけ | 新しい InterventionEvent を足して投影時に KeyError（握られて黙って出ない） |
| `test_event_id_is_deterministic_per_episode_and_seq` | 再投影が OpenSearch の同じ `_id` になる（設計 §14 の再構築） | 純粋関数 | ID が乱数・衝突して再投影が重複文書を作る／別の行を黙って捨てる |
| `test_formatter_uses_a_supplied_projection_event_id_only_when_well_formed` | 整形器は決定的 ID を 32桁 hex の時だけ採用 | 実の整形器を通す | 任意の文字列が `_id` になり Collector の退避・自動 ID に化ける |
| `test_append_event_is_logged_only_after_commit` | log-contract §9 と同じ commit 後の発行・相関項目 | store → session → 整形器の実経路 | commit 前に出て、rollback した変更がログにだけ残る |
| `test_rolled_back_event_is_never_logged` | 同上（rollback） | 同上 | 正本に無いイベントが検索に出る |
| `test_only_reference_like_refs_reach_the_log` | INV-39（本文・prompt を出さない） | 実の整形器の出力文字列を検査 | 他担当が refs に本文を入れた時にログへ流れる |
| `test_logging_failure_does_not_break_the_commit` | INV-38（ログの故障で正本の書き込みが失敗しない） | 故障を注入して commit を見る | 投影の例外が `append_event` を通して業務の transaction を壊す |
| `test_reproject_reemits_from_the_db_with_the_same_ids_and_marks_rows` | 再投影は live と同じ ID・`projected_at` を付け、2回目は出さない | DB → 整形器の実経路 | 再投影が別 ID で重複する／未投影の印が付かず毎回全件出す |
| `test_reproject_can_select_an_episode_and_a_seq_range` / `test_reproject_pages_through_more_rows_than_the_batch` | 範囲指定・頁送り（keyset） | 同上 | 頁の境界で行を落とす・重複する／他 Episode を出す |
| `test_reproject_rejects_a_seq_range_without_an_episode` / `test_script_refuses_a_seq_range_without_an_episode` | seq は Episode 内の通番 | 引数検査 | 全 Episode に seq 範囲を当てて意図しない行を出す |
| `test_script_reprojects_unprojected_rows_then_nothing` | `scripts/intervention-reproject.py` の既定（未投影だけ）・`--dry-run`・`--all` | script を読み込み SQLite の factory を渡す（Settings を読まない） | 運用手順（runbook §3）の command が実装と食い違う |

## 3. 台本の結び直し（`tests/unit/test_intervention_rebind.py`、SQLite・メモリストア・fake 生成器）

ADR-0041 §6 / 設計 §17-7。`script.edit_scenes`（文だけの修正）で storyboard を作り直さず、
`revision_links(kind=rebound_to)` で新台本へ結び直す。production / voice / render / scene_recovery の
「storyboard は現行台本から」の検査は `infrastructure/intervention/rebind.py:bound_script_ref` だけを通る。
有料 API は呼ばない（`FakeImageGenerator` の submit 回数と予約行の数で「課金なし」を見る）。
結び直しが無い経路は既存のテスト（production / voice / render / scene_recovery。変更なし）が従来どおり守る。

| テスト | 守る契約・事故 | 層の理由 | 捕まえる失敗 |
|---|---|---|---|
| `test_without_a_link_the_storyboard_source_script_is_used` | 結び直しが無ければ従来の `storyboard.source_script` | 解決関数を直接 | 既存 Episode・日次実行の挙動が変わる |
| `test_the_latest_rebound_link_wins` | 複数回の修正では最後の結び直しが現行 | 同上 | 古い修正版の台本で音声を作る |
| `test_a_link_to_another_episodes_script_is_rejected` / `test_a_link_to_a_non_script_is_rejected` | link を書いた側を信用せず毎回検査する | 同上 | 誤った link で他 Episode の台本・別型の成果物を読む |
| `test_structure_check_accepts_text_only_edits_and_rejects_layout_changes` | 結び直してよいのはシーンの id・並び・尺が同じ台本だけ（構成変更は storyboard の作り直しが要る） | 純粋関数 | 尺の変わった台本に古い storyboard の映像を当て、音声と映像がずれる |
| `test_structure_check_rejects_a_visual_edit` / `test_production_plan_refuses_a_rebound_script_with_another_visual` | 結び直しで許すのは文（ナレーション・題・hook）だけ。`visual` の変更は拒否（クロスレビュー HIGH） | 純粋関数 + 計画 Activity | 映像の指示を変えた台本を結び直し、storyboard・画像が旧い映像のまま「反映済み」に見える |
| `test_production_plan_follows_the_rebound_script` | 計画は結び直し後の台本を使い、画像・動画の作業（storyboard）は変わらない。link 無しは従来どおり | Activity を直接（Temporal 不要） | 計画が旧台本のまま／storyboard が別物になる |
| `test_production_plan_refuses_a_rebound_script_with_another_layout` | 構成の違う結び直しは needs_input（`ProductionInputInvalidError`） | 同上 | 検査が解決関数の外に漏れて計画が通る |
| `test_narration_edit_regenerates_voice_but_reuses_paid_images` | 有料の画像は再利用（予約行・submit が増えない）、音声だけ新台本から作り直す | 画像・音声 Activity と PaidJobRunner の実経路 | 台本修正で有料素材を作り直す（二重課金）／旧台本の音声を流用する |
| `test_voice_without_a_link_still_refuses_a_storyboard_from_an_older_script` | link 無しでは従来の拒否（文言も同じ） | Activity を直接 | 結び直しの導入で既存の安全検査が緩む |
| `test_render_inputs_accept_the_rebound_script_only_when_passed_in` | domain は純粋のまま、解決済みの参照を Activity から受け取る。既定は従来の検査 | 純粋関数 | domain が DB を読む／既定で検査が緩む |
| `test_render_uses_the_rebound_script_and_keeps_the_scene_videos` | 新台本の音声・マニフェストから描き直し、シーン動画は同じ Artifact | Render Activity（fake エンジン） | 結び直した Episode が render で止まる／動画を作り直す |
| `test_render_without_a_link_still_refuses_a_script_the_storyboard_was_not_made_from` | link 無しは従来どおり `RenderInputIntegrityError` | 同上 | 既存の整合検査が緩む |

## 4. 停止の失敗分類（`tests/unit/test_intervention_hold_classification.py`）

dispatch gate（担当 B。`mark_dispatched` / `mark_upload_dispatched` の中）は **stub** する
（`ProviderReservationRepository.mark_dispatched` を monkeypatch。repositories.py は編集しない）。
B の実装が入った後は、統合 branch の integration test が実物の gate で同じ性質を見る。
結果: 既存の分類（`NeedsInputError` の継承から型名表を導出）だけで条件を満たし、**コードの変更は不要だった**。

| テスト | 守る契約・事故 | 層の理由 | 捕まえる失敗 |
|---|---|---|---|
| `test_gate_errors_are_needs_input_and_never_retried` | 停止は needs_input・retry しない | 型名表は workflow 側の唯一の分類 | 停止が transient 扱いになり、Activity が retry し続ける |
| `test_gate_errors_are_not_reconciliation_or_round_errors` | 停止は照合待ち・待ち直し・新ラウンド・代替映像案のどれでもない | workflow の型名集合を直接見る | 停止で新ラウンド（新しい予約・課金）や代替案（Codex 呼び出し）が始まる／ログで unreconciled と誤分類 |
| `test_activity_boundary_keeps_the_type_and_stops_retries` | Activity 境界で型名を保ち non_retryable | `raise_activity_error` を直接 | 境界で別の型に包まれ分類が変わる |
| `test_held_dispatch_leaves_an_unsent_reservation_that_resume_reuses` | 停止時は provider へ送らず予約は reserved・未 dispatch。再開で同じ予約を使い、新しい予約を作らない（ADR-0041 §3・INV-15） | `PaidJobRunner` の実経路（SQLite・fake 生成器）で gate だけ stub | 成否不明（`ProviderSubmitAmbiguousError`）に包まれ照合待ちで永久停止／再開で二重予約 |
| `test_held_dispatch_blocks_the_episode_once_without_retry_or_recovery` | Production workflow は1回の submit で blocked（failed でない）・代替案に入らない | time-skipping の Temporal 試験サーバ + 名前登録の mock Activity | workflow がラウンドを重ねる／failed（終端）にして再開できなくする |

## 5. DB とオブジェクトストアの整合検査（`tests/unit/test_intervention_consistency.py`、SQLite・メモリストア）

設計 §14「バックアップと復元で RDB の参照とオブジェクトの整合性を検査する」・§17-11。検査器
（`infrastructure/intervention/consistency.py`、`scripts/consistency-check.py`）は読み取りだけ。
実の PostgreSQL + MinIO での復元演習は `scripts/intervention-backup-drill.sh`（隔離スタック）で行う。

| テスト | 守る契約・事故 | 層の理由 | 捕まえる失敗 |
|---|---|---|---|
| `test_a_consistent_state_has_no_problems` | 全ての表に行がある整合した状態で誤検知しない | 本物の store・表・fixture（媒体つき Artifact 一式） | 検査が常に NG で、復元の判定に使えない |
| `test_missing_and_changed_objects_are_reported` | オブジェクト欠落・中身の違い（Artifact・agent_runs の snapshot） | 同上で1件ずつ壊す | MinIO の複製漏れ・別バージョンの上書きを見逃す |
| `test_missing_media_inside_an_artifact_is_reported_unless_skipped` | Artifact JSON の中の媒体本体（動画・音声）も検査。`--skip-media` で省ける | 同上 | 記述子だけ残り「履歴はあるが再生できない」（設計 §14）を見逃す |
| `test_dangling_intervention_references_are_reported` | manifest の members・parent、revision_links、episode_controls の参照が解決する | 同上 | DB の一部の表だけ復元・FK の無い参照の欠落 |
| `test_a_reference_into_another_episode_is_reported` | 参照先が同じ Episode | 同上 | 別 Episode の成果物を採用 manifest に入れた破損 |
| `test_script_exit_codes` / `test_script_rejects_unknown_arguments` | 不整合で非0、出力形式、直す引数を持たない | script を読み込み fixture を渡す | 演習 script が NG を見落とす（終了コード0） |

## 6. backup/restore 演習（`scripts/intervention-backup-drill.sh`、隔離スタック・実の PostgreSQL 17 / MinIO）

unit test（§5）はメモリストアと SQLite なので、`pg_dump`/`pg_restore` と `mc mirror` を通した後も参照と sha256 が
保たれることは言えない。演習は実物で通し、**検査が壊れた復元を見つける**ことまで確かめる（1オブジェクト削除 → 終了コード 1）。
本番 `avp2` は script が拒否する。元データは `tests/support/intervention_world.py:seed_world`（unit test と同じ形）を
`scripts/intervention-backup-drill-seed.py` が隔離 DB へ入れる（`AVP_ENVIRONMENT=test` でなければ拒否）。
pytest に入れていないのは、`docker compose exec`（postgres・minio コンテナ）を test-runner の中から呼べないため。
