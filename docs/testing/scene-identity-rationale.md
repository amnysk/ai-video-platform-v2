# INV-33（シーン単位の同一性）のテスト設計根拠

事故の形: ADR-0034 は 422 対策として prompt 組み立て規則の版を 1→2 に上げた。画像・動画の
`input_hash` は版を含み、再利用ゲートは hash の完全一致でしか既存を見つけないため、
版を上げたままデプロイして停止中の Episode を再開すると、成功済みの全シーンが再生成・再課金される
（`tests/integration/test_incident_recovery_e2e.py::test_422_..._version_bump_mitigation` が実証）。
本番の成果物・予約はすべて旧方式（版 v1）の hash を持つ。

## 純粋関数（`tests/unit/test_scene_identity_v2.py`、unit）

| テスト | 守るもの / 落ちたら何が起きているか |
|---|---|
| `test_recipe_family_strips_only_the_prompt_builder_version` | 伏せるのは末尾の `prompt-v<N>` だけ。生成器の版（`fake-image-profile-v1`、fal のモデル profile）まで伏せると、モデルを変えても古い画像を再利用してしまう |
| `test_recipe_version_candidates_enumerate_every_earlier_version` | 旧方式の照合は版 1〜現在を全部試す。本番の行は v1、ADR-0034 以降は v2 で作られうる |
| `test_v2_image_hash_is_in_a_different_namespace_from_the_legacy_hash` | 方式1と方式2の hash は衝突しない（旧行を新方式の一致と誤認しない） |
| `test_image_input_hash_changes_with_the_recipe_version_but_content_fingerprint_does_not` / `test_video_content_fingerprint_ignores_only_the_motion_prompt_version` | input_hash は版に敏感なまま（新しく作るシーンは新しい版で作る）、content fingerprint は版に鈍感 |
| `test_image_content_fingerprint_changes_when_the_content_changes` / `test_video_content_fingerprint_changes_when_the_content_changes` | 版以外の材料（シーンの内容・生成器・storyboard・元画像・尺）が変われば必ず作り直す。特に動画は元画像の sha256 に依存し、代替案で画像を作り直せば動画も作り直す |

## Activity 経由の再利用（`tests/unit/test_scene_identity_reuse.py`、unit: SQLite + メモリストア + fake）

本物の Activity・`PaidJobRunner`・再利用ゲート・台帳を通す（どこか1箇所だけ hash 方式が古いと
素通りするので、関数単体では足りない）。Temporal も Docker も要らないので unit に置く。

| テスト | 守るもの |
|---|---|
| `test_image_recipe_version_bump_reuses_every_succeeded_scene` | **事故の核**。版を上げても成功済みの画像は provider に再 submit されない。未作成のシーンは新しい版で作る |
| `test_video_recipe_version_bump_reuses_the_succeeded_video` | 動画も同じ（`motion_profile_id` は `VideoProductionActivities` 全シーン共有だった） |
| `test_override_regenerates_only_the_overridden_scene_image` | 代替映像案を立てたシーンだけが再 submit され、そのプロンプトは代替案の文面から組み立てられる。他シーンは再利用 |
| `test_legacy_image_artifact_is_reused_without_a_new_submit` / `test_legacy_video_artifact_is_reused` | 本番の旧方式の行（`content_fingerprint` NULL、旧方式の hash と冪等キー）を作り直さない。停止中 Episode の再開で既存素材を再課金しない |
| `test_unmatched_legacy_image_artifact_is_not_reused` | 旧方式で再計算しても一致しない行は再利用しない（旧行なら何でも使う、にならない） |
| `test_legacy_image_of_an_overridden_scene_is_not_reused` | 代替案のあるシーンでは旧方式の元画像（= provider に拒否された画像）を再利用しない |
| `test_in_flight_legacy_reservation_is_resumed_not_resubmitted` | hash 方式が変わっても、旧方式で submit 済みの課金ジョブを await で再開し、二重 submit しない |
| `test_legacy_rejected_input_is_not_resubmitted_under_the_new_hash` | 本番の 422 行（旧方式 hash・`input_rejected_by_provider`）が、新方式の hash で「予約なし」と誤認されて同じ画像を再送しない（INV-32 と組み合わせて効く） |
| `test_recipe_tolerance_forgives_only_the_prompt_builder_version` | 検証（ADR-0033）の版チェック緩和は prompt 版だけ。生成器が違えば緩和しても `VERSION_MISMATCH` |

## 本番データでの照合（テストではなく一度きりの読み取り確認、2026-09-29）

本番 DB（SELECT）と本番 MinIO（GET）から、Episode `ec44fadd` / `dedcf315` の現行 scene_image 24 行・
scene_video 4 行を読み、旧方式の関数で再計算した hash が保存 `input_hash` と 28/28 一致（すべて版 v1）。
422 で拒否された動画予約 2 行（`dedcf315` sb4 / `ec44fadd` sb2）も旧方式 hash で一致。
実データはテストに入れていない（fixture は合成）。
