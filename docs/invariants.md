# 不変条件（Invariants）

このファイルは**契約**であり、要約ではない。ここに書かれた条件を破る変更は
ADRなしにマージしてはならない（[AGENTS.md §6](../AGENTS.md)）。

- 接頭辞 `INV-` は全体で1系列。**番号は再利用しない。**
- 廃止した条件は削除せず「廃止（日付・理由）」を残す。
- 各条件には「機械検査」欄がある。`未検査` と書かれた条件は
  **願望であって保証ではない**。その上に別の設計を積まないこと。

## A. 呼び出し方向（結合）

### INV-1 UIはWorkerを直接呼ばない
Next.js / ブラウザから `workers/` のコードや Temporal task queue へ直接届く経路を作らない。
UIの入口は FastAPI のHTTP APIだけ。
**機械検査**: `tests/architecture/test_layering.py`（未実装 / Phase 1）

### INV-2 SchedulerはWorkerを直接呼ばない
cron・timer 等のスケジューラは Temporal の schedule/workflow start だけを行い、
Activity や worker 関数を直接呼ばない。
**機械検査**: 未検査

### INV-3 Workerは他のWorkerを直接呼ばない
`workers/<a>/` から `workers/<b>/` へのimportを禁止する。共有したいロジックは
`domain/` か `infrastructure/` へ降ろす。
**機械検査**: `tests/architecture/test_layering.py`（未実装 / Phase 1）

### INV-4 Workerは次のJobを決めない
Workerは与えられたJobを実行して結果を返すだけ。次に何を実行するかの判断は
Workflow定義（Temporal）のみが持つ。Worker内に「成功したら次は◯◯」を書かない。
**機械検査**: 未検査（レビュー項目）

### INV-5 TemporalがWorkflow実行を担当する
工程の順序・retry・タイムアウト・補償は Temporal workflow に表現する。
アプリ側に独自のスケジューリングループや状態ポーリングを作らない。
**機械検査**: 未検査

### INV-6 レイヤ依存は一方向
`apps/ → domain/, contracts/, infrastructure/`、
`workers/ → domain/, contracts/, infrastructure/`、
`domain/ → contracts/` のみ。
`domain/` は `apps/` `workers/` `infrastructure/` をimportしない。
**機械検査**: `tests/architecture/test_layering.py`（未実装 / Phase 1）

## B. 状態と永続化

### INV-7 PostgreSQLがapplication stateのsource of truth
Episode / Job / Artifact のドメイン状態はPostgreSQLが権威。
Temporal・MinIO・UIキャッシュの値を権威として読まない。
**機械検査**: 未検査

### INV-8 Temporal内部状態とapplication domain stateを分離する
workflow execution status（running/completed/terminated 等）を
Episode状態へ直接写像しない。両者は独立に進み、対応表を持たない。
UIに出すのはdomain state。
**機械検査**: 未検査

### INV-9 MinIOがArtifact本体を保持する
バイナリ（動画・音声・画像）をPostgreSQLに入れない。DBが持つのは
参照（bucket/key/etag/size）とメタデータのみ。
**機械検査**: 未検査

### INV-10 Artifactはversionとschemaを持つ
すべてのArtifactは `schema_name` と `schema_version`、および
同一論理成果物内での単調増加 `version` を持つ。スキーマ無しの成果物を作らない。
**機械検査**: `tests/contract/test_artifact_schema.py`（未実装 / Phase 1）

### INV-11 Artifactはimmutable
一度書かれたArtifactオブジェクトは上書きしない。作り直しは新しい `version` を作る。
**機械検査**: 未検査

## C. 失敗とretry

### INV-12 retry可能なJob失敗だけでEpisodeをterminal failedにしない
Jobの失敗は必ず [failure-policy](./failure-policy.md) の失敗クラスへ分類される。
`retryable` / `needs_input` クラスの失敗でEpisodeを `failed`（terminal）へ落とさない。
分類できない失敗は自動修復に流さず、人間の判断待ち（`blocked`）にする。
**機械検査**: `tests/unit/test_failure_policy.py`（未実装 / Phase 1）

### INV-13 1つのJobの失敗が他のEpisodeを止めない
Episode間に暗黙の直列依存を作らない。あるEpisodeの停止は
他のEpisodeのworkflow進行に影響しない。
**機械検査**: 未検査

### INV-14 Upload処理はidempotentである
同じ `(episode_id, artifact_version)` に対するuploadは、何度実行しても
最大1件のYouTube動画しか生成しない。冪等キーを永続化してから外部呼び出しを行う。
**機械検査**: `tests/unit/test_upload_idempotency.py`（未実装 / Phase 1）

### INV-15 課金を伴う外部呼び出しは予約を先に永続化する
provider呼び出しの前に予約レコードをcommitする。プロセスがクラッシュしても
未照合の予約が残り、**自動で再送も解放もしない**。
**機械検査**: 未検査

## D. 実行モデル

### INV-16 FastAPI上で重い処理を同期実行しない
API handlerは検証・永続化・workflow start/signal だけを行う。
動画生成・レンダリング・アップロード・外部AI呼び出しをリクエスト内で待たない。
**機械検査**: `tests/architecture/test_api_no_heavy_work.py`（未実装 / Phase 1）

### INV-17 Activityは冪等である
Temporalは同じActivityを複数回実行しうる。全Activityは再実行に耐えるか、
冪等キーで重複を吸収する。
**機械検査**: 未検査

### INV-18 テストとCIから有料API・実投稿を呼ばない
外部provider（fal.ai / YouTube）へ到達しうるコードパスは、テストでは
必ずfake/adapterで置換する。
**機械検査**: `tests/architecture/test_no_live_calls.py`（未実装 / Phase 1）

## E. 外部副作用（前身repoから継承・非交渉）

### INV-19 YouTube投稿は private のみ
public/unlisted への自動切替、既存投稿の変更・削除・再投稿をしない。
公開は所有者の手動判断。
**機械検査**: 未検査

### INV-20 secretを出力しない
APIキー・OAuthトークンをログ・Artifact・トレース属性・コミットに出さない。
**機械検査**: 未検査
