# ADR-0045: 運用者 UI（P2-B）と `/episodes/*` の opt-in 認証

## Status

Accepted（2026-10-16）。前提は [ADR-0044](./0044-ui-mcp-shared-application-backend.md)。flag は既定 OFF のまま。
本番反映・MCP Tool 化は対象外。

## Context

ADR-0044 は 13 操作を `/v1/intervention` に出したが、人が使う画面が無い。また残リスク 1（`/episodes/*` が未認証）と
3（cursor が `created_at` のみ）を P2-B で解消すると約束した。JS のビルド基盤は repo に無い
（`package.json` なし）。`/episodes/*` は `scripts/smoke*.sh` と多数のテストが認証なしで呼ぶ。

## Decision

1. **UI は依存ゼロの静的 HTML + ES modules**（`apps/web/static/`）。FastAPI が `/ui/` で配信する。
   `JPMAS2_APPLICATION_API` が OFF のとき `/ui/*` は 404 `feature_disabled`（API と同じ flag、同じ拒否）。
   ビルド工程・フレームワークは足さない。同一 origin なので CORS は不要。
2. **UI は `/v1/intervention` だけを呼ぶ。`/episodes/*` は呼ばない。** 業務ロジックは UI に置かない。
   操作の名前・method・path・scope・人限定・入力は `contracts/application_operations.py` から生成した
   `apps/web/static/operations.json` を読む。生成物との一致は contract test が検査する（OpenAPI 断片と同じ方式）。
   UI だけの操作は作らない（13 操作のまま）。
3. **認証**: 既存の Bearer principal をそのまま使う。画面でトークンを入力し `sessionStorage` にだけ保持する
   （タブを閉じると消える。localStorage・cookie は使わない）。UI のボタン非表示は権限制御ではない。
   権限はサーバー側（ADR-0044 §2 `_authorize`）が強制し、UI は 401/403 の理由をそのまま表示する。
4. **`/episodes/*` の opt-in 認証**: 設定 `EPISODES_API_AUTH_REQUIRED`（既定 false = 現行互換）。true のとき
   GET は scope `read`、POST は scope `resume`（作成・再開・起動系）の principal を要求し、無ければ 401/403。
   `/healthz` は対象外。smoke スクリプトは既定のまま動く。本番で true にするのは所有者の判断。
5. **cursor**: `list_episodes` に `before_id` を足し、`(created_at, id)` の複合比較にする。応答に `next_before_id`。
   `before_id` が無ければ従来動作（後方互換）。絞り込み `status`（EpisodeStatus）と `q`（topic の部分一致）を足す。
   いずれもカタログの入力の追加であり、操作は増えない。
6. **表示の原則**: 記録が無い項目は「未記録」と表示する。推測・補完しない（`LLM 費用 = unmeasured`、
   `rationale_kind = NONE` などはその旨をそのまま出す）。
7. **監査表示**: 正本は DB（`episode_events`・`intervention_commands`・`decision_records`・`change_sets`）。
   UI は workspace の `read_model` からこれらを時系列で表示する。OpenSearch は読まない（投影であり欠落しうる）。
   command の `payload` 全文は表示せず、型・対象・理由（`reason`）・revision 参照だけを出す。
   token・secret は元々 read model に無い。
8. **負荷**: 一覧は軽い `list_episodes` のみ。詳細は初回と操作後に workspace を 1 回読み、経過表示は PG のみの
   `progress` を 10 秒間隔で、タブが前面のときだけ polling する。workspace の polling はしない。

## Alternatives

- React 等の導入: ビルド基盤が無く、依存と供給網のリスクが増える。却下。
- `/episodes/*` の即時認証必須化: smoke・既存テストを壊す。opt-in にして所有者が切り替える。
- 監査を OpenSearch から読む: 投影で欠落しうる。DB を正本にする（ADR-0040）。
- 14 番目の「監査取得」操作: 既存の workspace で足りる間は足さない。重ければ別 ADR。

## Consequences

- 良い: 既存の認可・冪等・課金ガードをそのまま通る。UI を差し替えても業務は変わらない。
- 悪い / 負債:
  - workspace は全量読み（操作後 1 回）。専用の軽い read は未導入。
  - 戦略・演出方針・Research の専用 read 操作は無く、read model に出る範囲だけを表示する（無い項目は「未記録」）。
  - トークンを人が貼る方式。ログイン/セッション管理は未実装（単一運用者の想定）。
  - `EPISODES_API_AUTH_REQUIRED` を false のままにすると残リスク 1 は残る（既定の互換維持と引き換え）。
  - 素材アップロード・`terminal_failed` job の画面からの復旧は未実装。
  - 追記: read model の `CommandView` に `reason`（`payload.reason` が str のときだけ、1000 文字まで。payload 全体は出さない）と `correlation_id` を足した。監査画面はこれを表示し、無ければ「未記録」。`trace_id` は引き続き `result`/`refs` に実在する場合だけ。

## Review fixes（独立レビュー後の追記）

- **`/episodes/*`・`/research/*` の POST は人限定**（§4 の修正）: ON のとき scope だけでなく
  `actor_type=human` も要求する。`ai_assistant`/`agent` は `resume` を持っていても 403
  `forbidden`（`reason=human_only`。ApplicationService と同じ code・文言の形）。GET は `read` のみ（AI 可）。
  OFF は従来と同一。`create_app()` の route 表（OpenAPI）から全 `/episodes*`・`/research*` を列挙するテストで、
  依存の付け忘れを検出する。
- **既知・受容**: `/docs`・`/redoc`・`/openapi.json`・`/healthz` は ON でも認証しない（スキーマと生死確認のみで、
  業務データ・副作用を持たない）。公開したくない環境では reverse proxy 側で絞る。テストで現状を固定した。
- **有料の宣言は `OperationSpec.paid` に一本化**: `start_agentic_production` / `resume_production` /
  `apply_approved_change` が `paid=True`（ADR-0044: resume は固定計画を歩き、未完了 stage で有料生成しうる）。
  `operations.json` に `paid` を出し、UI の `PAID_OPERATIONS` リテラルは廃止。再開ダイアログは有料警告と
  確認チェックを要求し、権限ヒントにも「有料」を出す。集合は contract test で固定する。
- **`before_id` だけの cursor は 422**: `ListEpisodesRequest` が `before` との対を検証する（黙って先頭ページに戻らない）。
- **一覧の「さらに読み込む」は、最後に「検索」を押した時点の絞り込みを使う**（入力欄だけ編集中の値は使わない。`cursor.js` の `createSearchFilters`）。
- **render の世代管理**（`render_gate.js`）: `render()` は await のあと古ければ捨て、直前の view を必ず dispose する。遅い応答が新しい画面を上書きして poller を漏らすことは無い。
- **control_revision の固定**: hold/resume/propose はダイアログを開いた時点の `control_revision`（`pinControlRevision`）を送る（apply はプレビュー時の値を固定済み）。古ければサーバーが stale として拒否する。detail の `loadWorkspace` は世代管理し、古い応答が新しい応答を上書きしない。
- **2xx は失敗として表示しない**: 送信が 2xx で返ったあと、本文が空・想定外で結果を描画できなくても「送信は受理されました。画面の更新に失敗」を出し、送信ボタンは戻さず、workspace は読み直す。
