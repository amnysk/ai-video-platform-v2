# 運用者 UI（P2-B）

設計は [ADR-0045](../decisions/0045-operator-ui-and-episodes-api-auth.md)。この文書は画面の使い方と、
実装上の約束事だけを書く。

## 構成

`apps/web/static/` を FastAPI が `/ui/` で配信する（`JPMAS2_APPLICATION_API` が OFF なら 404）。
依存ゼロ・ビルドなしの HTML + ES modules。CSP は `default-src 'self'` のため、inline script / style /
`on*` 属性 / 外部 CDN・フォントは使えない（`tests/contract/test_web_static_csp.py` が検査）。

| 場所 | 役割 |
|---|---|
| `index.html` / `app.css` | 殻。ライト/ダーク両対応、狭い幅でも横スクロールしない |
| `js/main.js` | hash routing、ログイン/一覧/詳細の切替 |
| `js/api.js` | `/v1/intervention` への fetch。Bearer、`X-Correlation-ID`、401 でトークン破棄、ETag は返ってきたときだけ |
| `js/state/session.js` | トークン保持（`sessionStorage` のみ） |
| `js/operations.js` | `operations.json`（backend が生成）の読み込み。path・scope は常にこれから取る |
| `js/errors.js` | エラーコード → 日本語の理由と対処 |
| `js/status.js` / `js/stages.js` / `js/timeline.js` / `js/cursor.js` / `js/requests.js` / `js/poller.js` / `js/router.js` / `js/format.js` | DOM を使わない純ロジック（`node --test` で検査） |
| `js/views/*` | 画面（`list` / `detail` / `stage_card` / `audit` / `interventions` / `dialog` / `login`） |

## 画面

- **UI-A `#/` 一覧**: トピック・状態（ラベルと生の値）・種別（Agentic/Legacy）・作成/更新。トピック検索（`q`）、
  状態絞り込み（`status`）、「さらに読み込む」は `before` + `before_id` → `next_before` + `next_before_id`
  で、client 側でも `episode_id` で重複を除く。「要注意」バッジは `js/status.js` の `NEEDS_ATTENTION`
  だけが決める（`failed` / `blocked` / `needs_work` / `ready_for_review`。いずれも `contracts/states.py` の
  `EpisodeStatus` の値で、テストが一致を検査する）。
- **UI-B `#/episodes/{id}` 詳細**: Research / Strategy・Brief / Script / Storyboard / Production / Render /
  Upload の 7 ステージ。入力・出力・AI の判断（候補・採用・rationale_kind・rationale・evidence_refs・
  rejected_reason・uncertainties）・model/tool/token・job 状態・失敗と復旧の可否。ステージへの振り分けは
  `js/stages.js` の定数表だけが決め、表に無いものは「ステージ未分類」に置く（捨てない）。
  workspace は開いたときと操作成功後に 1 回読み、`progress` だけを 10 秒ごと（タブが前面のときだけ、
  画面を離れたら停止）に読む。
- **UI-C 介入**: hold / resume / 絵コンテ変更の提案（`visual_description` のみ）/ 影響プレビュー /
  承認して適用 / 制作開始（有料・人のみ）。読み取りは絵コンテ・品質レビュー・影響プレビュー。
  すべて `<dialog>` の確認を通り、ヘッダに対象 Episode ID を出し、送信直前に URL の ID と読み込み済み
  workspace の ID が同じか確かめる。
  - `command_id` は開いたとき 1 回作り、通信失敗・5xx の再試行では同じものを使う。サーバーが確定的に
    拒否（4xx）したあとの再送は新しい操作として新しい `command_id` にする。送信中は submit を無効化。
  - `expected_control_revision` は最新の workspace（適用は影響プレビュー）の値。`stale_*` は
    workspace を読み直して理由を説明し、ダイアログはやり直しを促して閉じるだけにする。
  - 適用は影響プレビューを先に読み、見積りが無い（`null` / 未見積り）・blocker がある・上限額が空/見積り未満
    ならば送らない。上限額に既定値は入れない。
  - ボタンの出し分けは権限制御ではない。principal の scope を知る API が無いので、常に押せる状態で出し、
    必要な scope は「各操作に必要な権限」に示す。権限判定はサーバー。
- **UI-D 監査・履歴**: `episode_events` / `intervention_commands` / `decision_records` / `change_sets` を
  時系列に併合。主体（人 / AI / Agent / システム）とステージで絞り込み。command は型・状態・主体・
  reject_reason・時刻・revision 参照だけで、payload/result の全文は出さない。trace id と correlation id は
  そのフィールドが実在する場合だけ表示する（command の reason / correlation_id は `CommandView` から）。

## トークンの扱い

画面で貼ったトークンは `sessionStorage` にだけ保存する（タブを閉じると消える）。`localStorage`・cookie・URL・
DOM・ログには出さない（`console.*` は使わない）。401 を受けたらトークンを破棄してログイン画面に戻る。
「トークンを破棄」ボタンでも消せる。

## 「未記録」の規則

記録が無い項目は必ず `未記録` と表示する。推測・補完・既定値の埋め込みはしない。`0` / `false` は記録された値。
- LLM の費用は `unmeasured`（計測されていない。0 円ではない）とそのまま表示する。
- 費用の見積りが `null` のときは「未見積り（0 円ではありません）」。
- `rationale_kind = none` は「理由の記録なし」。
- 読み取り時に導出された判断（`derived`）は保存された記録ではないと明示する。
- command の `reason`（hold の理由のみ）と `correlation_id` は read model の `CommandView` の項目。無ければ `未記録`。
  event の `trace_id` 等は `result` / `refs` に実際に入っていない限り `未記録`。

## 縮退

`operations.json` が読めないときは、path を手書きで補わず「操作カタログを読み込めません」と再試行を出す。

## テスト

テストの意図は [docs/testing/jpmas2-p2b-rationale.md](../testing/jpmas2-p2b-rationale.md)（§6 以降）。
