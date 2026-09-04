# 旧repo資産の棚卸し

対象: `/home/yoshiki/projects/ai-video-pipeline`（**参照専用。変更しない**）

分類:
**A) そのまま再利用可能** / **B) Adapter化して再利用可能** / **C) 新設計では再実装**

移植は「そのままコピー」ではなく **B を既定**とする。Aと判定したものも、
`infrastructure/providers/` の Protocol の背後に置くこと（INV-18）。

## 1. YouTube API（アップロード / OAuth）— **A**

| ファイル | 規模 |
|---|---|
| `youtube_uploader/api.py` | 362行 resumable upload、429/5xx retry、再開状態照会 |
| `youtube_uploader/auth.py` | 98行 OAuth 2.0 + PKCE(S256)、ローカルコールバック |
| `youtube_uploader/common.py` | 83行 scope定義、token refresh、0600 atomic write |
| `youtube_uploader/cli.py` | 165行 CLIエントリ |

依存が**標準ライブラリのみ**（`urllib` / `http.client`）で、旧worker本体への
importがゼロ。パッケージごと持ち込んでActivityから呼べる。
resumable uploadの中断再開照会は INV-14（投稿の冪等性）にそのまま使える。

## 2. YouTube Analytics / metrics — **B**

- `youtube_uploader/api.py` の `ANALYTICS_METRICS` / `video_analytics()` — A相当
- `studio_dashboard/metrics.py`（87行）`MetricsFetcher` — **B**。
  `ok / basic_only / insufficient_data / no_credentials` の4状態degradeモデルは
  設計として優秀で移植価値が高い。ただし認証情報をローカルJSONから読む前提なので、
  Secret/DB由来のトークンへ差し替える
- `youtube-analytics-mcp/` — **C**。ソース消失（`.pyc` のみ、git未追跡）。
  移植前に別バックアップの有無を所有者に確認すること

## 3. fal.ai / AI API adapter — **A（コア）+ B（プロファイル）**

| ファイル | 規模 | 判定 |
|---|---|---|
| `studio_dashboard/fal_queue.py` | 624行 | A |
| `studio_dashboard/seedance.py` | 193行 | A |
| `studio_dashboard/fal_images.py` | 61行 | A |
| `studio_dashboard/fal_video_profiles.py` | 167行 | B |
| `studio_dashboard/fal_image_profiles.py` | 65行 | B |

既にprovider非依存に抽出済みで、抽象点は `_extract_output_url` /
`_build_submit_payload` の2つだけ。HTTP transportが注入式（`FalTransport` Protocol）。
**「submit前にreceiptを永続化」「曖昧な中断は再送しない」「bounded pollで
receiptをsubmittedのまま残す」という二重課金防止のセマンティクスは
自力で再発明すると必ず金銭事故になる。**INV-15 の実装の下敷きとする。

要修正: `FX_RATE_JPY_PER_USD = 158.0` のハードコード、receiptがファイル前提
（→ PostgreSQL へ）。

## 4. prompt 資産 — **A（最優先級）**

- `.codex/agents/*.toml`（17体、計237行）— コード依存なし、丸ごと持ち込める
- `AGENT.md` / `CLAUDE.md` / `strategy/`（channel-strategy、beat-structure-patterns、
  shorts-growth-strategy、reviews/、learnings.jsonl）— **再作成不能な運用知**
- `studio_dashboard/agents.py`（299行）の `ROLE_PIPELINE`（役割×ステージ×trigger）— **B**。
  Temporal workflow定義に読み替える設計図として読む

注意: TOMLが `docs/prompt-templates.md` を参照しているが**そのファイルは実在しない**
（参照切れ）。移植時に補完すること。

## 5. render 処理 — **主に C**

- `studio_dashboard/runner.py`（**7,363行**）に render 関連が埋没 — **C**。
  OpenMontage・DB・budget が密結合したgod-object
- `studio_dashboard/openmontage_view.py`（195行）— **B**。artifact JSONを
  読むだけのconsume-only境界。契約の参考資料として価値大
- `studio_dashboard/music.py`（104行）— **A/B**。BGMカタログのsha256検証と
  「カタログ外を拒否」の権利安全ロジックが小さく完結。パスをMinIOキーへ
- `studio_dashboard/frame_normalize.py`（237行）— **B**。純関数寄り
- `studio_dashboard/paths.py`（51行）— **C**。symlink探索前提
- TTS本体 — 旧repoに実装なし（OpenMontage側）。**C（新設計で実装）**

## 6. upload 処理 — **B**

- `youtube_uploader/worker.py`（292行）— **B**。指数backoff、承認検証、
  コードdrift検知は良い。ただし `queue/` → `uploaded/` のディレクトリ移動＋
  sentinelファイルでのプロセス間通信は Temporal + PostgreSQL が構造的に置換 → 骨組みは **C**
- `studio_pipeline/release.py`（229行）— **B**。
  `REQUIRED_REVIEW_GATES = (historical_accuracy, rights, editorial, final_render)` と
  privacy検証は移植価値が高い（INV-19 の実装に使う）
- `state/`（29件のJSON）、`uploaded/`（約540MB）— 過去データ。
  メタJSONのスキーマは新DB設計の実サンプルとして参照
- `systemd/*.service|*.timer`（5本）— **C**（Temporal workerが置換）

## 7. テスト

| ファイル | 規模 | 判定 |
|---|---|---|
| `tests/test_seedance.py` | 689行 | **A** — fal queueのsubmit失敗/中断/resume/二重課金防止。**移植価値トップ** |
| `tests/test_frame_normalize.py` | 314行 | **A** — 純関数 |
| `tests/test_design_contracts.py` | 3,410行 | **B** — 定数を持ち込むなら一緒に |
| `tests/test_short_drama.py` / `test_series_drama_quality_gates.py` | 449 / 330行 | **B** — 品質ゲートの判定基準そのものが資産 |
| `tests/test_production_profiles.py` | 296行 | **B** |
| `tests/test_dashboard.py` | 10,757行 | **C** — runner/serverの巨大結合テスト |
| `tests/test_series_*.py` | 約1,700行 | **C** — SQLite前提 |
| `test_park_ownership.py` / `test_daily_*.py` | 計2,200行超 | **C** — 旧repo固有の運用ゲート |

## 8. Artifact関連 — **B**

- `studio_dashboard/production_profiles.py`（473行）— `required_artifacts` と
  `artifact_definitions`（成果物名×生成ステージ×必須性）。
  **`contracts/schemas/` の直接の下敷き。最有力の移植対象**
- `studio_dashboard/contracts.py`（949行）— `RENDER_OUTPUT_RELPATH`、
  `DeliveryGeometry`、`TEXT_OVERLAY_TYPES`、`RENDER_BLOCK_CLASSES` 等の**語彙集**。
  定数群は A、sentinel/fingerprint系は C
- `studio_dashboard/store.py`（988行）— **jobsテーブルはTemporalが完全に代替 → C**。
  ただし **`cost_entries` の reserve→commit→reconcile→release モデルは A/B**
  （INV-15 の実装にほぼそのまま使える）
- `seedance.py` の `build_manifest()` → `asset_manifest` 契約 — **B**
- `content/`（20+エピソードの成果物）— **データ資産**。few-shot と回帰fixtureに使える

## 移植優先度トップ5

1. **`youtube_uploader/` 一式**（543行）— 外部依存ゼロ、ほぼ無改造でActivity化できる
2. **`fal_queue.py` + `seedance.py` + `fal_images.py` + `tests/test_seedance.py`** —
   二重課金防止のreceipt/resumeセマンティクス。receiptの永続先だけPostgresへ
3. **`.codex/agents/*.toml` + `AGENT.md` + `strategy/`** — 再作成不能な運用知
4. **`production_profiles.py` + `contracts.py` の語彙部分** — Artifactスキーマの下敷き
5. **`store.py` の cost_entries + `metrics.py` の4状態degrade + `release.py` の承認ゲート** —
   「金」「計測」「公開可否」の3つの安全弁

## 明確に移植しないもの

`runner.py`(7,363行) / `automation.py`(4,679行) / `server.py`(910行) /
`tests/test_dashboard.py`(10,757行) / `systemd/` / sentinelファイル方式の
プロセス間協調全般 / `series_*` 群。
これらは Temporal + FastAPI + PostgreSQL が**構造的に**置き換える層である。
