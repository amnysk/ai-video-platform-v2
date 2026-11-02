# BGM 自動化の安全面のテストの設計理由（ADR-0087 §9 / §10。INV-158 / INV-159）

テストを足すたびに、なぜそれがテストになったか（守る事故・その段階を選んだ理由）を同じコミットでここに足す。

## 投稿経路の監査（INV-158）

`AVP_QUALITY_GATE_ENFORCE` が ON のとき、YouTube に触れる全経路が同じ gate に従うことを経路ごとに示す。

| # | 経路 | gate を通るか | どう示すか |
|---|---|---|---|
| 1 | 通常の Episode Workflow（`EpisodePipelineWorkflow` → `upload_gate` → `UploadWorkflow`） | はい（自動の規則。REVIEW も拒否） | `test_quality_gate_persisted_wiring.py`（`upload_gate`）。加えて #3 の門が `upload` Activity の中にもある |
| 2 | 手動 API `POST /episodes/{id}/upload`、`scripts/quality-approve.py`（同じ `start_upload` を通る） | はい（手動の規則。REVIEW は所有者の承認を記録してから起動） | `test_quality_gate_persisted_wiring.py::test_manual_*`、`test_quality_approve_script.py` |
| 3 | `UploadWorkflow` の直接起動（Temporal CLI・スクリプト）。`upload_gate` も API も通らない | **以前は通らなかった（迂回）**。`upload` Activity に最後の門を足して塞いだ | `test_upload_path_quality_gate.py`（`upload_final_video` を直接呼ぶ = 直接起動と同じ入口） |
| 4 | `upload` Activity の retry・再配送 | はい（Activity の中の門が毎回走る。spent は gate より前に既存の受領を返す） | `test_upload_path_quality_gate.py`、`test_upload_double_upload_paths.py::test_a_retried_activity_*` |
| 5 | 過去 Episode の再投稿 | `uploaded` は `admit` が入れない。それ以外（`render_ready` / 自分が止めた `needs_work`）は #3 の門 | `test_upload_double_upload_paths.py::test_an_uploaded_episode_is_not_admitted_again`、鮮度 `test_a_past_pass_*` |
| 6 | その他 | `scripts/` に YouTube を呼ぶ投稿の入口は無い（`smoke_upload.py` は fake・読み取りのみ）。`ensure-daily-schedule.py` は DailyEpisode を起動するだけで #1 に入る | `grep -rn "start_upload_workflow\|UPLOAD_WORKFLOW" apps scripts workers` の結果（API の 1 箇所と pipeline の子 workflow だけ） |

**多層防御の位置**: `UploadActivities._upload` の、spent 判定（既に投稿済みなら YouTube を呼ばない）と一時停止の確認の後、
`WorkDirectory`・ダウンロード・予約の前。ここより後ろは YouTube に触れうるので、拒否は予約も session も作らない。

- `test_a_refused_decision_never_reaches_youtube`: BLOCK / REVIEW / 作成失敗 / 評価エラー / 期限切れ / 必須欠落 / 宣言欠落の
  各理由コードで、fake の session・chunk・動画・予約行が全て 0。理由コードを足したのに通ってしまう退行を、全列挙で捕まえる。
  拒否は `UploadQualityRefusedError`（needs_input・non_retryable）で、retry が YouTube を叩き続けない。
- `test_a_service_that_raises_is_a_refusal_not_a_pass` / `test_enforce_on_without_a_service_fails_closed`: 判定できないものを
  PASS に倒さない（fail-closed）。サービスの組み立て漏れ（ON なのに `None`）も拒否で、黙って無検査になる事故を止める。
- `test_the_real_gate_without_usage_declarations_is_a_review_and_refuses`: 代役ではなく本物の `QualityGateService` と DB で、
  宣言の無い動画が直接起動でも通らないことを見る（stub だけだと配線の漏れを見逃す）。
- `test_a_producer_block_never_reaches_youtube_even_with_an_owner_approval`: BLOCK は承認の行があっても解放されない（INV-129）。
  承認の判定を「行があれば通す」に緩める退行を捕まえる。
- `test_a_review_with_the_owner_approval_recorded_proceeds` / `..._delegated_agent_approval_is_refused` /
  `test_an_approval_of_another_episode_does_not_release_this_one`: 手動 API が書いたのと同じ承認の行（所有者・同じ Episode・
  現行 final_video の hash）だけが REVIEW を解放する。新しい迂回は作らず、既存の記録を読むだけ。agent の委任・別 Episode は不可。
- `test_enforce_off_never_touches_the_service_or_the_quality_tables` / `test_default_construction_is_the_pre_existing_behaviour`:
  OFF（既定）は導入前と同一（INV-130）。サービスに触れず、品質の 4 表に 1 行も書かない。引数の既定値のままでも従来どおり。
- `test_a_past_pass_is_reused_*` / `..._refused_once_it_is_stale_*` / `..._does_not_cover_a_changed_final_video`: INV-126。
  同じ hash の PASS は再検査せず再利用し、期限切れで再検査できなければ拒否、別の動画は前の記録を引き継がない。
  サービス層の既存テスト（`test_quality_gate_service.py`）が個別に守る性質を、投稿の入口で通しで確かめる。

## 二重投稿（`test_upload_double_upload_paths.py`）

gate が PASS でも拒否でも、台帳・マーカー・upload key が先に二重を止める。

- `test_a_retried_activity_after_a_successful_upload_makes_no_second_video`: YouTube の成功後に Activity が再配送されても動画は 1 本。
- `test_a_spent_reservation_blocks_a_second_upload_even_when_the_gate_now_refuses`: 投稿済みの受領は gate の判定が変わっても
  そのまま返る（YouTube を呼ばない）。gate を spent の前に置くと、投稿後の保存に失敗した Episode が永久に拒否される。
- `test_a_lost_response_then_a_retry_never_makes_a_second_video`: 動画は作られたが応答の前に worker が落ちた場合。
- `test_a_dispatched_reservation_without_a_result_holds_even_with_a_passing_gate` /
  `test_an_existing_marker_video_is_adopted_not_uploaded_again`: 結果不明の送信済み予約は新しい round を開かず、
  マーカーで見つかった動画は採用して再投稿しない（INV-109）。
- `test_an_uploaded_episode_is_not_admitted_again`: 投稿済みの Episode は `admit` で入れない（再投稿の経路が閉じている）。
- `test_upload_gate_refuses_a_spent_reservation_before_the_quality_service`: 自動の門も spent を先に見て、検査を走らせない。

## 課金の安全（`test_bgm_auto_billing_safety.py`）

BGM を付けた再描画を 3 通り・gate の拒否を全理由・投稿の成功と再配送を続けて回しても、`provider_reservations` に増えるのは
YouTube の投稿の 1 行だけで、fal の行は 0、fal クライアントの呼び出しは 0 回（呼ばれたら失敗する spy）。
描画の中身（フォールバックの梯子）は描画側のテストの持ち物で、ここは台帳と workflow の層だけを見る。
「BGM を変えたら課金される」「gate が拒否して retry するたびに予約が増える」という前身 repo 型の事故（AGENTS.md §9）を、
台帳の行数で直接固定する。

## flag の連鎖（`test_bgm_flag_chain_guard.py`、compose・release のテスト。INV-159）

- ガードは純関数にして全組み合わせを表で見る（BGM×ENFORCE、ACQUISITION×BGM）。ON にできない順序を起動時に止めるのが目的で、
  実行時に黙って検査なしの BGM が出る事故（ENFORCE が OFF の BGM は投稿前に検査されない）を防ぐ。
- render / upload worker が実際にガードを呼ぶことは AST で見る（関数だけ作って呼び忘れる退行）。
- `test_with_every_flag_off_*`: 全 OFF の組み立てが導入前と同一（品質サービス・writer を作らない。収集 worker は profile 外）。
- compose: 収集 worker は profile `bgm-acquisition` だけ（`--profile core` と deploy-workers に入らない）。
  deploy-workers.sh で名前を挙げて `up` すると compose はその profile を自動で有効にして既定で起動してしまうため、
  `OPT_IN_SERVICES` として deploy の対象から外し、その一覧が実際に core 外の build 付き service と一致することを検査する
  （`test_deploy_workers.py` の変更は、この service を除外する分だけ。理由はコミットメッセージにも書いた）。
  secret（FAL_KEY・YouTube）は持たせない。`GEO_MAPS_ENABLED` は全 service で既定 false のまま。
- release record: 新 flag を `FEATURE_FLAG_ENV_KEYS` に足したので、`.env` / compose の実効値と突き合わせる既存の仕組みに乗り、
  委任された承認では ON にできない（`test_release_feature_flags.py` は列挙が増えた分の変更）。

## workflow の replay（`test_bgm_auto_replay.py`）

workflow のコードは変えていない（`workflow.patched` 不要）。変えたのは Activity の中身と、返しうる失敗の型だけ。
(1) 変更前に採った履歴（`tests/unit/fixtures` の upload・pipeline・render・daily）が今のコードで replay できること、
(2) 新しい型 `UploadQualityRefusedError` で終わる UploadWorkflow の履歴を time-skipping で作って replay できること
（non_retryable なので retry せず `record_failure(needs_input)` で終わる）を固定する。本番の実履歴での replay は
staging の手順（`docs/operations/staging.md` §5）で行う。

## AGENTS.md §8 の grep（新しい設定キー `bgm_acquisition_enabled` / `BGM_ACQUISITION_ENABLED`）

実行: `grep -rn "bgm_acquisition_enabled\|BGM_ACQUISITION_ENABLED" apps/ workers/ domain/ infrastructure/ contracts/ docs/`
（この文書を除く。tests と compose も別に数えた）。

| 分類 | 件数 | 内訳 |
|---|---|---|
| 書き手（定義） | 2 | `infrastructure/config.py`（`Settings.bgm_acquisition_enabled`）、`contracts/music_sources.py`（キー名の唯一の定義） |
| 読み手（コード） | 1 + 1 | `infrastructure/music/guard.py`（メッセージ文字列のみ。判定は引数）、`contracts/release.py`（`BGM_ACQUISITION_SETTING_KEY.upper()` で env 名を導く）。収集 worker の `run_worker` は別の枝（A）が `require_bgm_for_acquisition` を呼ぶ |
| 読み手（compose） | 1 | `compose.yaml` の `bgm-acquisition-worker` だけ（`${BGM_ACQUISITION_ENABLED:-false}`） |
| 設計書 | 3 | `docs/decisions/0087`（§10）、`docs/decisions/README.md`、`docs/invariants.md`（INV-159） |
| テスト | 約 10 | compose 契約・release flag・guard |
| 無関係 | 0 | 別の意味で同名の語は無い |

env 名は `contracts.music_sources.BGM_ACQUISITION_SETTING_KEY.upper()` から導き、`contracts/release.py` 以外に literal を書いていない
（テストと compose の `${...}` と文字列メッセージを除く）。
