# Worker 版の統一: 実測と、各テストが存在する理由

対象: `scripts/deploy-workers.sh` / `scripts/workers-versions.sh` / Dockerfile の revision label /
`worker_entry` の起動ログ（ADR-0024 追補、`docs/operations/workers.md` §10）。

## 1. 実測（2026-09-20 15:00 JST 時点、稼働中コンテナ）

各コンテナの `/app`（contracts / domain / infrastructure / workers / prompts / apps / alembic.ini /
pyproject.toml）の全ファイルの sha256 を、`git archive <commit>` の同じパスと突き合わせた。
一致数が全ファイルに達する commit を「そのコードが入っている」とした。

| サービス | image id | 入っているコード | e29c7e8 と一致 |
|---|---|---|---|
| script / storyboard | 64fb067a | **e29c7e8**（186/186 一致） | はい |
| pipeline / production-image / production-video | c3cddfca | **f38835c**（185/185）。dd40c54・e29c7e8 の前 | いいえ（11 ファイル差） |
| production / production-voice / render / upload | 86c9ea9a | **5a1a0cd**（165/165）。topic-planner 系の前 | いいえ（20 ファイル差＋topic 関連ファイル欠落） |
| api | c311b28c | **8f55bc4**（178/178） | いいえ（6 ファイル差） |
| dummy-worker | 2a3cb197 | **5a1a0cd**（162/162） | いいえ |

- どのイメージも未コミット WIP（`.worktrees/claude-production-autopilot` の 2026-09-20 01:00 以降の変更）
  とは一致しない。WIP は稼働していない
- `docker images` の `avp2-worker:local` は 64fb067a（00:08 JST ビルド）1つだけ。古い image id の3つは
  タグを失った（dangling）まま、コンテナだけがそれを使い続けている
- 差の中身: render-worker には `dd40c54` / `e29c7e8`（英語ナレーションの尺調整）だけでなく、
  `contracts/topic*.py`・migration 0008/0009・`domain/storyboard/coverage.py` 等も入っていない

### 根本原因

共通イメージは1枚（`avp2-worker:local`）だが、デプロイが「サービスごとの `build` / `up -d <service>`」の
繰り返しだった。`up -d` は指定したサービスしか作り直さないので、指定されなかったコンテナは
古い image id のまま動き続ける。さらに版を示す情報（label / ログ）が無く、混在に誰も気付けなかった。

## 2. テストの意図

実 Docker（本番）はテストから触らない（AGENTS.md §9）。スクリプトは PATH 先頭の `docker` / `git` shim
（呼び出しを記録し、固定の状態を返す）で実行し、手順の順序・フック・失敗時の後始末を固定する。

### tests/contract/test_deploy_workers.py（契約: 設定ファイル同士の一致）

| テスト | 守るもの | 落ちる例 |
|---|---|---|
| `test_app_services_cover_every_service_built_from_this_repo` | 「全サービスを列挙する」（部分適用の再発防止）。compose に `build:` 付きサービスを足したのに `APP_SERVICES` に足し忘れると落ちる | worker を足して古いまま残る |
| `test_services_sharing_an_image_tag_share_one_build_spec` | 同じタグを名乗るサービスの build が食い違うと、後からビルドした版がタグを奪う | `target` だけ違う worker |
| `test_build_services_build_each_image_tag_exactly_once` | ビルドは各タグ1回（`BUILD_SERVICES`）。タグが漏れるとそのイメージだけ古い | `avp2-app` を誰もビルドしない |
| `test_deploy_never_builds_or_recreates_infrastructure` | 別 worktree から `up` すると postgres の相対 bind mount が変わり再作成される。`--no-deps --no-build` を必須にする | postgres が再作成される |
| `test_makefile_deploys_only_through_the_script` | 手順の入口を1つにする | Makefile が独自の `docker compose up` を持つ |
| `test_app_and_worker_images_carry_revision_label` | 版を label で確認できる（`app` と `worker`） | label が付かず NO-REVISION |
| `test_base_stage_does_not_depend_on_the_revision` | 実測: base に ARG / LABEL を置くと revision ごとに worker の apt / npm / piper（約 1 分）が再ビルドされる。revision は最終 stage だけ | base に ENV を足す |
| `test_revision_is_declared_after_the_heavy_layers_of_the_worker_stage` | 同上（worker stage 内でも重い層より後ろ） | ARG を apt の前に移す |
| `test_compose_builds_api_migrate_dummy_from_the_app_stage` | target が `base` のままだと label が付かない | api が `target: base` に戻る |
| `test_versions_script_reads_revision_label` | 確認スクリプトが label 名を取り違えない | label 名の typo |

### tests/unit/test_deploy_scripts.py（振る舞い: shim で実行）

| テスト | 守るもの |
|---|---|
| `test_versions_*`（5件） | STALE（image id ≠ タグ）・revision 混在・revision 不明（label 無し / unknown）・期待 revision との不一致を、それぞれ exit 1 で検出する。今回の事故そのもの（3 commit の混在）の検出器 |
| `test_deploy_success_runs_hooks_in_order_and_recreates_everything` | PRE → build（各イメージ1回、revision を渡す）→ migrate → 全サービス up → POST(success)。infra は up の対象外。build が up より先 |
| `test_deploy_refuses_dirty_tree_before_touching_anything` / `..._only_with_explicit_override_and_marks_revision` | dirty からの本番デプロイを既定で拒否（何のコードが動いているか不明になるため）。許すなら revision に `-dirty` が残る |
| `test_failed_build_stops_before_recreate_but_post_hook_still_runs` | ビルド失敗でも POST が `failure` で必ず走る。フックに置いた後始末（例: 一時停止の解除）が残らないことの固定点 |
| `test_failed_pre_hook_aborts_before_build_and_post_hook_runs` | PRE 失敗は何も変えずに中止し、部分的に実行された PRE の後始末（POST）は走る |
| `test_unhealthy_worker_fails_the_deploy_and_post_hook_reports_failure` | 起動しただけで成功にしない。healthy にならなければ非0、POST は failure |
| `test_failed_migrate_stops_before_workers_are_recreated` | migrate 失敗のまま worker を新版にしない |
| `test_stale_container_after_recreate_fails_the_deploy` | 全サービスを up しても作り直されなかったコンテナがあれば失敗（最終ゲート） |
| `test_unhealthy_infrastructure_is_refused_before_building` | `--no-deps` で infra を起動しないため、落ちていれば何も変えず中止 |
| `test_post_hook_failure_makes_an_otherwise_good_deploy_fail` | 後始末の失敗を成功と報告しない |

### tests/unit/test_worker_entry.py（追加2件）

`test_start_log_reports_image_revision` / `test_start_log_marks_unknown_revision`: 稼働中 worker のログから版を辿れる
（`AVP_GIT_REVISION`、無ければ `unknown`）。

## 3. 検査していないこと（未検証）

- 実 Docker での deploy の完走（本番を触るため。デプロイ実施時に `make deploy-workers` の出力で確認する）
- Dockerfile のキャッシュ挙動は、テストではなく手元の `docker build` 実測（revision だけ変えた再ビルドで
  worker stage の全層が CACHED になること）で確認した
