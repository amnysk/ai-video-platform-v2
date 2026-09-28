# ADR-0035 のテスト設計根拠

事故: 2026-09-26/27 の fal 422 `content_policy_violation`（`body.image_url`、実在人物の肖像）で
日次 Episode が止まり、ADR-0034 の緩和策（prompt 版を上げる）は全シーンを再課金することが
連続試験で分かった。以下は各テストが何を守るか。

## 共有基盤（`tests/unit/test_scene_visual_foundation.py`、unit）

純粋な契約と関数なので unit で固定する。DB も provider も要らない。

| テスト | 守るもの / 落ちたら何が起きているか |
|---|---|
| `test_old_storyboard_scene_without_visual_subject_still_parses` | 本番に残る旧 storyboard が読めること。落ちれば停止中 Episode の再開が契約違反で止まる |
| `test_storyboard_scene_accepts_a_visual_subject` / `test_visual_subject_vocabulary_is_closed` | 映像対象は閉じた語彙。自由文字列を許すと「何を描くか」の判定・集計ができない |
| `test_rejected_input_vocabulary` | 拒否対象の語彙が DB の CHECK（migration 0014）と一致する前提 |
| `test_scene_visual_override_round_trips_through_parse_artifact` | 代替案が `parse_artifact` の唯一の入口で読めること（INV-10） |
| `test_scene_visual_override_rejects_incomplete_plans` | 根拠（`rationale`）・対処した拒否（`rejection_ids`）の無い代替案、revision 0 を保存しない。人間が後で判断できる記録であることを契約で強制する |
| `test_apply_override_replaces_only_visual_fields` | 代替案は映像の項目だけを替え、尺・台本対応を替えない。落ちれば音声・字幕・尺まで動き、無関係な成果物が無効化される |
| `test_apply_override_without_override_is_identity` / `..._for_another_scene_is_refused` | 代替案の無いシーンは不変、別シーンの案は適用できない |
| `test_fingerprint_ignores_position_on_the_timeline` | 前のシーンの尺が変わっただけで後ろのシーンが作り直しにならない |
| `test_fingerprint_changes_when_what_the_scene_shows_changes` | 映像の内容が変われば必ず新しい指紋（古い画像の誤再利用を防ぐ。ADR-0017 の「文面を変えたら版を上げる」の代わり） |
| `test_fingerprint_is_per_scene` | **事故の核の裏返し**。別シーンの内容は指紋に入らない。落ちれば1シーンの差し替えが全シーンを再課金する |
| `test_fingerprint_of_a_scene_without_subject_is_stable_across_the_new_optional_field` | 任意項目の追加で旧シーンの指紋が変わらない（旧 Episode の再利用互換） |
| `test_recovery_limits_are_positive_and_scene_limit_fits_episode_limit` | 上限の定義元が1箇所にあり、矛盾しない（INV-34 の前提） |

## migration 0014（`tests/contract/test_migration_frozen_vocabulary.py`、contract）

| テスト | 守るもの |
|---|---|
| `test_0014_downgrade_vocabulary_is_frozen_at_8a8c499` | downgrade 先の語彙が履歴どおり（enum が将来変わっても変わらない） |
| `test_0014_upgrade_adds_exactly_the_adr_0035_values` | 追加は3語だけ。今の enum と DB の CHECK が一致 |
| `test_0014_scene_scope_matches_the_models` | 新しい3語はシーン単位（scene_id 必須） |
| `test_0014_downgrade_restores_every_upgraded_check` | 張り替えた CHECK は downgrade で全部戻る |
| `test_0014_legacy_rejection_classification_reads_both_summary_formats` | 本番に実在する2行（`dedcf315` sb4 / `ec44fadd` sb2、pydantic repr 形式）を `image` と判定する。一度きりの補完が誤判定すると、停止中 Episode の拒否画像が再送される |

既存テスト2件（`test_0004_scene_scope_types_are_frozen`、`test_0006_upgrade_adds_exactly_the_phase6_values`）は、
今の enum ではなく 0014 が凍結した「前」の値と比べるよう変えた。これはファイル内の既存の前例
（0005・0006 追加時の同じ書き換え）どおりで、仕様変更は ADR-0035 で承認している。
