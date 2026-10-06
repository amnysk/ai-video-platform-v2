# ログ基盤（ADR-0040）の検証の土台 — なぜこの試験があるか

担当C（障害試験・回帰・運用手順）が `deploy/logging/test/` に置いた試験用の部品と、その理由。
試験の台本は [docs/observability/verification-plan.md](../observability/verification-plan.md)。

## 1. 隔離 app スタック（`deploy/logging/test/compose.apptest.yaml` / `run-e2e.sh`）

**なぜ要るか**: 既存の integration テストは `TEMPORAL_ADDRESS` / `MINIO_ENDPOINT` /
`TEST_DATABASE_URL` を読み、本番スタック（compose project `avp2`）の postgres・temporal・minio に
test DB・test namespace・test bucket で接続する作りになっている（ADR-0021）。ログ基盤の試験では
テストを何度も・障害を注入しながら回すので、本番の Temporal サーバや MinIO に負荷・残骸を残す
経路を最初から持たないことにした。

- 独自の postgres / temporal / minio を持つ別 project（`avp2-oslog-c*`）。`avp2` は拒否する
- ホストへポートを公開しない。network は `internal: true` で**外部へ出られない**。
  有料 provider（fal.ai）・YouTube へは物理的に到達しない（INV-18。fake の取り違えがあっても
  課金・投稿が起きない二重目の囲い）
- パスワードは実行時に乱数で生成し repo 外（`$XDG_RUNTIME_DIR`、0600）に置く。
  生成したパスワードはそのまま秘密情報の非漏洩試験（S-SEC）の needle にも使える
- `.env` がツリーにあれば拒否する（`Settings()` が `.env` を読み、FAL_KEY 等が混入し得るため）
- 本番のイメージ tag（`avp2-worker:local` / `avp2-app:local`）は上書きしない（別 tag でビルド）
- コードは worktree を read-only で `/src` に mount する。A・B の成果を merge した後に
  イメージを焼き直さずに同じ手順で回せる
- test-runner は json-file（max-size / max-file / labels）と label `avp.logging=app`、
  `AVP_SERVICE_NAME` / `AVP_ENVIRONMENT=test` を持ち、ADR-0040 §1 の経路（stdout → json-file →
  Fluent Bit）にそのまま乗る。`docker compose run` の one-off コンテナは `--rm` しない
  （Collector が読み終わる前にログファイルが消えるのを防ぐ。`down` で消す）

### 実測で見つけたこと

- `WorkflowEnvironment.start_time_skipping()` は初回に `temporal.download` から test server を
  取得する。internal network では取得できず、`test_episode_workflow.py` / `test_script_workflow.py` /
  `test_storyboard_workflow.py` の 29 件が setup error になった（1回目: 108 passed, 1 skipped,
  29 errors, 400s）。ビルド時に SDK 自身に取得させてイメージへ焼き、実行時に tmpfs の `/tmp`
  （`exec` 付き。既定の tmpfs は noexec）へ写すことで解消した（外部への通信を許さずに済む）。
- `tests/integration/test_pipeline_schedule.py` は `TEMPORAL_ADDRESS = "localhost:7233"` を
  **ハードコード**しており、env の接続先を読まない。隔離スタックでは skip になる。ホストで
  `pytest tests/integration` を走らせると、本番スタックが公開している `localhost:7233` に
  （test namespace で）接続する。AGENTS §3 に従いここでは直さず記録だけする。

## 2. 障害注入・検証スクリプト（`deploy/logging/test/`）

いずれも**雛形**（フェーズ1）。B の `compose.logging.yaml` の env 名・サービス名・volume 名が
確定したら `lib.sh` の既定値を合わせる。検証の合否は OpenSearch への問い合わせ（`search-assert.py`）
と Fluent Bit の metrics で機械的に判定し、目視に頼らない。

| ファイル | なぜ要るか |
|---|---|
| `loggen.py` | A の実装を待たずに B の経路（tail・型検査・Bulk・重複抑制・rotation）を試すための、契約どおりの行と壊した行の発生器。`request_id=<tag>` で件数を数え、`--duplicate-every` で同じ `event_id` の再出力（文書 ID の重複抑制）を作る。壊した行は毎回作り直し、409（同一 ID）と型不整合を混同しない |
| `search-assert.py` | 全走査（`absent`・`fields`）は scroll（`_doc` 順）で回す: search_after の sort に `event_id` を使うと、`event_id` の無い infra の index で 400 になり infra の非漏洩を確かめられなかった（2026-10-06 実測）。`snapshot` は ISM policy の `last_updated_time` を `ism_template` の中も含めて比較から外す（同じ内容で policy を書き直しただけの差分を、設定の変化と取り違えないため。2026-10-06 に試験用 policy から本番 policy へ戻した後、内容は同じでもこの時刻だけが diff に出た）。`count --wait` は接続失敗（再起動直後の TLS EOF・接続拒否）も待つ。停止・復旧系の試験で、OpenSearch の起動待ちを件数不足と取り違えて即失敗しないため。件数・必須フィールド（`REQUIRED_APP_FIELDS` を契約から import。写しを作らない）・`event_id` 重複・`_source` 全走査の非漏洩・alias・ISM・版・bootstrap 前後の設定差分・取り込み遅れを TLS + 認証で assert。標準ライブラリだけで動く。パスワードはファイルからだけ読み、応答本文の値プレビューを出さない |
| `inject-secrets.py` | 秘密に見える値を**実行ごとに乱数で**作り（本物の秘密も repo 内の固定値も使わない）、message・キー名・例外 chain・第三者 logger・warnings・thread の未捕捉例外・logging を通らない print の各経路で出す。`attributes` は `enabled:false` で検索できないので、検査は `_source` の全走査で行う |
| `fault-opensearch-stop.sh` | OpenSearch 停止中にアプリ側が止まらず、再開後に全件・重複 0 で追いつくこと（INV-38、ADR-0040 §5 の retry 上限内） |
| `fault-collector-rotation.sh` | Collector 停止中の rotation を inode で続きから読めること（ADR の実測 300/300 を、この構成で再確認） |
| `fault-bad-lines.sh` | 不正1件による永久滞留が無いこと・Bulk 部分失敗で正常行が落ちないこと・型不整合の退避 |
| `fault-capacity.sh` | buffer 上限での古い chunk の破棄と検知。**容量制限した tmpfs でだけ**実行し、ホストのディスクを埋めない（tmpfs でなければ中止する）。出力速度を `rate` で絞り、Collector が json-file を読み切ってから（`catchup`）OpenSearch を戻す: 全速では json-file の rotation（20m × 5）が先に一巡し、buffer の破棄ではなく rotation の欠損を測ってしまう（2026-10-06 実測: 全速 200000 行で 118114 行が読まれる前に消え、buffer は 22MB で上限未達）。OpenSearch は healthy まで待ち、件数が 30 秒変わらなくなってから判定する（届き始めた直後の件数で合格にしない） |
| `fault-secrets.sh` | INV-39 を json-file・buffer・OpenSearch の3か所で確認。隔離スタックの実パスワードも needle に入れる。`--raw` の print は logging と同じ値を出すので、json-file は JSON 行（logging 経由）だけを 0 件の判定に使い、buffer は needle ごとの hit を出す（2026-10-06 の最終版試験で、全行を数えると print の 30 行が混ざり判定できなかった）。infra 系統に入るのは print・stderr だけなので、infra の logging needle の hit は raw と合わせて報告し、終了させない |
| `measure.sh` | 本番ホスト共存の条件（ADR-0040 §7 の MemAvailable 閾値）を実測で判断するための記録 |

## 3. test-runner で JSON ログを出す plugin（`apptest_logging_plugin.py`）と tool の実行ユーザー

- 既存の integration テストは `configure_logging()` を呼ばないので、そのままでは pytest プロセス内の
  Activity・Workflow のログは stdout に出ない（pytest の caplog にだけ入る）。テストの中身を変えずに
  本番と同じ整形器を通すため、`pytest -p apptest_logging_plugin` で読み込む plugin を置いた。
  A の `configure_logging` が無い間は何もしない（実測: A 実装前で
  `test_upload_workflow_persistence.py` 4 passed、結果は plugin 無しと同じ）。
- `inject-secrets.py` の値は host 所有者の 0600。rootless Docker ではコンテナ内 uid 1000 は subuid に
  写像され読めない（実測 PermissionError）ので、secrets を渡す tool だけ uid 0（= host の所有者、
  cap は全て落とす）で動かす。
- 陽性対照: `--stdlib-only`（伏せ字無し）で json-file に needle が 52 件出ることを確認済み。
  検出器が「0 件」を出すのが検出漏れでないことの裏付け。
