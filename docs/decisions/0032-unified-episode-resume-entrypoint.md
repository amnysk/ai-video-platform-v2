# ADR-0032: Episode の統一再開エントリポイント

## Status

Accepted (2026-09-23)

## Context

Production（ADR-0017 §8）・Render（ADR-0019）・Upload（ADR-0020）はそれぞれ独立の
`POST /episodes/{id}/{stage}` を持ち、各々が自分の admit 表で `blocked` / `needs_work` からの
再開を扱う。しかしこれらは互いを連鎖させない。

daily の正常系で工程を連鎖させているのは `EpisodePipelineWorkflow`
（`workers/pipeline/workflows.py:271-316`）**だけ**であり、これは Script→Storyboard→Production→
Render→Upload を子 workflow として順に**待って**実行する。ある工程が `STAGE_PARKING_STATUS` と
一致しない結果を返すと `_stop()` して `outcome=stopped` のまま終了する（ADR-0031 §Context）。
**この実行は終了しており、何かを待って止まっているわけではない**（調査で確認済み）。

したがって今回の事故を安全に再開するには、運用者は (1) Production を個別に POST で再開し、
(2) 成功を確認してから Render を個別に POST し、(3) 成功を確認してから Upload を個別に POST する、
という3段の手作業が要る。取り違えると工程を飛ばす／ Render を Production 前に呼んでしまう
（各エンドポイントは自分の admit 表だけを見るので入力が無ければ失敗するが、事前に確認する
手段が無い）。

`daily_episode_slots` はEpisode作成時にしか消費されない（`DailyEpisodeWorkflow` の
`claim_daily_slot` Activity のみが書く）。個別の POST 再開はこの Activity を呼ばないため、
今日時点でも日次枠の二重消費は起きていない。統一エントリポイントもこの性質を維持する。

## Decision

**(1) 読み取り専用の dry-run を先に置く。** `GET /episodes/{episode_id}/resume/plan` を新設する。
実装は「入力を読んで計画を返すだけの純粋関数」（`domain/pipeline/resume_plan.py` 想定）を
Activity 抜きで呼ぶ、または軽い読み取り専用リポジトリ呼び出しのみで構成し、
**Provider 呼び出し・予約 INSERT・workflow start を一切行わない**（コードレビューで機械的に
確認できるよう、この関数はネットワーク I/O を持つモジュールを import しない）。返す内容:

- 未解決 blocker（Episode の現在状態とその理由）
- 成否不明の Provider 予約（`provider_reservations.find_unreconciled` と同じ述語をここでも使う。
  判定を二重化しない）
- 実行予定の工程（現在の Episode 状態から admit 表をたどって導く。ADR-0017 §8 / ADR-0019 の
  既存の admit 判定関数をそのまま呼ぶ。表を作り直さない、AGENTS §8）
- 課金が起きうる工程の開示（`possible_new_charges`。**実装のスコープを縮めた**: 当初案の
  「再利用可能な Artifact」「欠落・破損・`schema_version`/`input_hash` 不一致の Artifact」
  「上流 Artifact の `input_hash` が現行の有料素材の `input_hash` と食い違う工程」という
  Artifact 単位の精密な diff は、工程ごとの Artifact・予約の追加リポジトリ配線が要り実装コスト
  が大きい一方、**安全性そのもの**（同じ入力のシーンを再課金しない）は dry-run の結果と無関係に
  Activity 層の `input_hash` 比較（INV-17）が既に強制している。したがって実装したのは
  「`stages_to_run` のうち `STAGE_PROVIDERS`（`domain/pipeline/resume_plan.py`）が空でない工程」
  という**開示**であり、「この工程には有料 provider が関わるので新しいラウンドが要れば課金され
  うる」以上を主張しない。実際に課金するかどうかの判定は退行させず Activity に委ねたまま
  （INV-17 の言い換えという当初の意図は保つ）。**Artifact 単位の精密な diff は将来の負債として
  残す**（§Consequences 参照）
- 再開できない場合はその理由（例: 他工程の未解決 blocker、成否不明の予約が残っている）

**(2) 実行は同じ計画関数を再検証してから行う。** `POST /episodes/{episode_id}/resume` は
再度 `(1)` と同じ関数（読み取りを取り直してから）を呼び直し（クライアントがキャッシュした
古い dry-run を信用しない。TOCTOU を避ける）、resumable でなければ 409 で理由を返す。
resumable なら `EpisodePipelineWorkflow` を**決定論的な workflow id**
（Episode 作成時と同じ命名規則 `pipeline_workflow_id`。新規に別の id 体系を作らない）で start
する。**実装ノート**: `WorkflowIDReusePolicy` は Temporal Python SDK の既定値
（`ALLOW_DUPLICATE`）のまま変えない。止まった実行は Temporal 上は `COMPLETED`
（アプリの `outcome=stopped` であって Temporal の失敗ではない）なので、
`DailyEpisodeWorkflow` が子 `EpisodePipelineWorkflow` を起動するときに使う
`ALLOW_DUPLICATE_FAILED_ONLY`（`workers/pipeline/workflows.py`）をここでも使うと、
一度でも止まった Episode は二度と再開できなくなる（`COMPLETED` は `FAILED_ONLY` の対象外）。
「実行中の同一 id を拒否する」性質自体は `WorkflowIDReusePolicy` の値に関係なく Temporal が
常に保証する（実行中の id への `start_workflow` は常に `WorkflowAlreadyStartedError`）ので、
既定値のままで §Decision の意図（同時実行の防止）は満たされる。アプリ側でロックを自作しない。
実行中であれば API は 409 を返す。

**(3) `EpisodePipelineWorkflow` は途中入場できる。** workflow 開始時に Episode の現在の domain
状態を読み、`PipelineStage` の列挙のうちどこから始めるかを、Production/Render/Upload の各
admit 表がそれぞれ既に持つ「この状態なら入れる」判定から導く（新しい遷移表を作らない。
`docs/domain/state-transitions.md` の遷移表に無い遷移は増やさない）。既に完了している工程は
スキップし、子 workflow を起動しない（＝再課金しない。INV-17）。

**(4) 日次枠を再消費しない。** 統一再開は `claim_daily_slot` Activity を呼ばない
（`DailyEpisodeWorkflow` を経由しない。既存の個別 POST 再開と同じ性質）。

**(5) 既存のガードを迂回しない。** 統一再開が呼ぶ経路は各工程の既存 admit Activity と同一であり、
`PAUSED` / `UPLOADS_PAUSED` 運用スイッチ・予算・投稿設定・品質検証はそこで既に評価されている前提を
崩さない（新しい判定を並行して作らない）。

**(6) retry と regenerate の区別。** 新しい状態やフラグを増やさず、`(1)` の dry-run が
「新たに課金が必要な工程」として明示することで区別を可視化する。実際の生成/再生成の判断は
既存の `input_hash` 比較（INV-17）にそのまま委ねる。

## Alternatives

**(a) API 層で工程順序を判断し、Production→Render→Upload を順に個別 POST する薄いラッパーにする** —
実装は簡単。しかし「次に何をするかは workflow だけが決める」（INV-4/5）に違反し、API が
工程順序の知識を持つ2箇所目になる（`EpisodePipelineWorkflow` と重複）。却下。

**(b) 止まった `EpisodePipelineWorkflow` の実行に signal を送って続行させる** — 調査の結果、
その実行は既に `completed` で終了しており、signal を受け取る対象が存在しない。却下（実装不可）。

**(c) 新しい `EpisodeResumeWorkflow` を別に作る** — `EpisodePipelineWorkflow` と工程順序の知識が
2つに分かれ、AGENTS §8（定義は1箇所）に反する。工程順序は既存の1つの workflow 定義に残す。却下。

**(d) dry-run を通常の POST の `?dry_run=true` パラメータにする** — 「read-onlyのdry-runから
Provider生成・予約作成・Workflow起動を行わない」ことをコードで保証しにくい（同じハンドラの
条件分岐に依存する）。GET の別エンドポイントにして、実装モジュールレベルで
I/O 発生源を分離する。採用。

## Consequences

**良い側**
- 運用者は工程を取り違えずに1つの入口から安全に再開できる。dry-run で事前に何が起きるか分かる
- 二重の再開要求は Temporal の `WorkflowIDReusePolicy` が構造的に防ぐ（アプリ側の自作ロック不要）
- 既存の admit 判定・予約台帳・input_hash 判定を再利用するだけなので、新しい二重化した意味論を
  増やさない

**悪い側 / 引き受けた負債**
- `EpisodePipelineWorkflow` の途中入場ロジックが増える分、workflow 定義の複雑度が上がる
  （工程数が増えるたびにこの判定も見直しが要る）
- regenerate は「新しい工程を明示的に選んで作り直す」操作としては提供しない。あくまで
  input_hash の食い違いに基づく自動判断のままで、「同じ入力でも作り直したい」という運用者の
  意図的な regenerate 要求には応えない（将来の負債として残す）
- 統一再開は `EpisodePipelineWorkflow` の途中入場に依存するため、この workflow 定義の互換性を
  崩す変更（例: `PipelineStage` の並びを変える）をするときは本 ADR の §Decision (3) を合わせて
  見直す必要がある
- `possible_new_charges` は工程単位の開示であって Artifact 単位の diff ではない（§Decision の
  実装ノート参照）。運用者は「この工程が走れば課金され**うる**」としか読めず、「実際に課金
  される／されない」をこの dry-run だけで確定できない。個々のシーンが再利用されるか新規生成
  されるかを事前に知りたい場合、現状は運用者が実行結果（`possible_new_charges` に載った工程の
  Artifact 生成ログ・予約台帳）を後から確認するしかない。Artifact 単位の精密な
  `input_hash` diff（`ArtifactMetadataRepository` / `ProviderReservationRepository` の追加読み取り
  で実現可能）は将来の拡張として残す
- **実装時に判明した既存の負債（本 ADR が作ったものではないが、統一再開の integration test
  の設計を制約した）**: `workers/pipeline/workflows.py::_stage_request` は Production/Render/
  Upload の**state workflow の名前と queue**（`PipelineOptions.production_workflow` 等）しか
  差し替えられず、各工程が内部で使うメディア Activity の task queue
  （例: `ProductionWorkflowInput.image_task_queue`、既定値 `production-image` 等）へは配線して
  いない。そのため `EpisodePipelineWorkflow` 経由で本物の `ProductionWorkflow` /
  `RenderWorkflow` / `UploadWorkflow` を動かす integration test は、共有の Temporal サーバ上で
  本物の provider 呼び出しに到達しうる本物の worker（docker-compose の
  `production-image-worker` 等）と queue が衝突する。統一再開の
  `tests/integration/test_episode_resume.py` はこれを避けるため、production/render/upload を
  専用 queue の fake workflow に差し替えている（§機械検査）。この負債を解消する
  （`PipelineOptions` に各工程のメディア queue も持たせる）のは本 ADR のスコープ外

## 機械検査

- `tests/unit/test_pipeline_workflows.py`
  （`test_starting_mid_pipeline_skips_earlier_stages_without_starting_their_children` /
  `test_starting_at_upload_still_applies_the_upload_gate`: `PipelineStage` の途中入場で
  それより前の工程の子 workflow を起動しないこと、UPLOAD ゲートが途中入場でも必ず経由される
  こと）
- `tests/unit/test_resume_plan.py`（`domain/pipeline/resume_plan.py` の純粋関数: 駐機点からの
  一意な再開先、`needs_work`/`blocked` の所有権判定、未照合予約のブロック、
  `possible_new_charges` の開示内容）
- `tests/unit/test_resume_api.py`（`GET/POST /episodes/{id}/resume*` の DI・エラー変換:
  dry-run が `WorkflowStarter` に触れないこと、409/404 の分岐、`start_pipeline_workflow` へ渡す
  引数）
- `tests/integration/test_episode_resume.py`（実 Temporal + 実 PostgreSQL。§Consequences の
  queue 制約により production/render/upload は専用 queue の fake に差し替え):
  `test_get_plan_dry_run_makes_no_writes_and_no_starter_calls` /
  `test_resume_reaches_upload_after_blocked_production_without_redoing_finished_scenes` /
  `test_two_concurrent_resumes_start_exactly_one_pipeline_execution` /
  `test_resume_does_not_bypass_uploads_paused`
- `tests/integration/test_production_rerun.py::test_failed_production_resumes_on_post_without_new_paid_submits`
  （既存・不変更。本物の `ProductionWorkflow` で「同じ input を二度課金しない」という核心の
  安全性そのものを検査する。統一再開はこの Activity 層の判定に乗るだけで判定を二重化しない）
