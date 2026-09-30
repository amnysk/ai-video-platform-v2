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
