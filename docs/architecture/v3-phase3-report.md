# v3 Phase 3 完了報告（品質管理・BGM・歴史地図の結線）

- 作成日: 2026-10-30（JST）/ 本番実測: 2026-10-29 16:30Z（UTC、read-only）
- 統合ブランチ: `claude/v3-phase3-integration`（土台 7ae20e8 = Phase 2 統合。本番 e3c9555 はその祖先）
- 監査: [v3-phase3-audit.md](./v3-phase3-audit.md) / 独立レビュー: [v3-phase3-review.md](./v3-phase3-review.md)
- **本番デプロイ: 未実施**（§4。所有者の作業と承認が要る）

## 1. 統合したブランチ

| ブランチ | 内容 | ADR / INV |
|---|---|---|
| `claude/v3-p3-audit` | 本番監査、integration テストの本番誤接続ガード、承認者の種別（所有者 / AI 代理）、API 認証、release preflight の flag・token 検査 | ADR-0079 / 0080、INV-121〜125 |
| `claude/v3-p3-quality` | 検査記録・使用宣言・品質レポート・手動承認の永続化（migration 0019）、必須検査の fail-closed、説明欄の入力生成 | ADR-0081、INV-126〜132 |
| `claude/v3-p3-bgm` | 承認済み曲のカタログ（content sha で再利用）、場面配置、ナレーション優先の ducking と発話マージン測定、ライセンス・クレジット検査 | ADR-0083、INV-133〜138 |
| `claude/v3-p3-geo` | GeoManifest（座標は gazetteer のみ）→ 決定論の検証と描画、Production 事前検査・Render 置換、fal 不使用 | ADR-0084、INV-139〜145 |
| `claude/v3-p3-wiring` | BGM / 地図の検査を QualityGate へ結線、Render の使用宣言、クレジットを説明欄へ、0019 の追記専用化 | ADR-0086、INV-146〜148 |
| `claude/v3-p3-review` | 独立レビュー文書と証拠テスト | — |
| （親） | AI 品質レビューの設計（Agent Runtime の LLM port / ModelPolicy を再利用、決定論が最終判定） | ADR-0085（設計のみ） |

新機能の flag（`AVP_QUALITY_GATE_ENFORCE` / `BGM_ENABLED` / `GEO_MAPS_ENABLED` / `AVP_UPLOAD_DESCRIPTION_COMPOSED`）は
すべて既定 OFF。OFF の挙動は e3c9555 と同じ（独立レビューで確認）。flag の無い安全側の変更は API 認証・承認者の種別・
integration ガード・release preflight の追加検査だけ。

## 2. 検証（統合 HEAD）

- ruff / format: 成功。pyright: 0 errors。
- `pytest tests/unit tests/contract tests/architecture`: 6399 passed / 0 failed（基準 5803）。
- Temporal replay（CI の 6 ファイル）: 83 passed。workflow・Activity 名・入出力 dataclass は不変。
- 隔離 integration（`run-e2e.sh`、project `avp2-oslog-c-p3e`、internal network・host port なし）: 169 passed / 6 skipped
  （skip は runner に docker CLI が無い `test_logging_collector_lua.py` の 6 件）。
- release preflight の `migration_compat`（target 統合 HEAD / deployed e3c9555、オフライン）: PASS。

## 3. 実素材での検証（本番 Episode 48f4eddb の read-only export、本番外で描画）

- BGM（合成曲。第三者音源なし）: 実 `RenderActivities` + 固定版 ffmpeg 7.1.1。積分 -16.7 LUFS・true peak -1.4 dBTP、
  発話中の声と BGM の差は最小 14.8 LU（閾値 12 LU）、映像ストリームは BGM 有無で同一、尺・同期一致。
  検査を結線した状態で技術 QA・説明欄・BGM の検査がすべて PASS、gate は `upload_allowed`。**fal 呼び出し 0・予約 0**。
  BGM のみの変更で production の成果物・input hash は不変（render だけ再実行）。
- 正直な評価: 音は人の耳で聴いていない。BGM は声の 15〜21 LU 下で、スマホのスピーカーでは聞こえにくい可能性がある。
  校正は 1 Episode・1 合成曲だけ。
- 地図: 屋島（1185）の正例は PASS（英語・日本語ラベル、1080x1920）。誤座標・時代錯誤の地名/境界・出典欠落の 6 例は
  すべて BLOCK。見た目は簡素（縮尺・方位記号なし）、海岸線は現代の Natural Earth（注記を必ず描く）。
  gazetteer と根拠はテスト用 fixture。

## 4. デプロイ判定

**判定: flag OFF のコードデプロイは安全（独立レビューの結論）。ただし今回は実施しない。** 理由:

1. 所有者の作業が要る: API の token ファイル（`~/.config/avp2/api/tokens`、0600、owner / agent の 2 行）。無いまま出すと
   変更系 API が 503 になる（日次の Schedule・watchdog は API を通らないので止まらない）。token は所有者が作る
   （エージェントが作ると所有者 / AI の区別が意味を失う。ADR-0080）。
2. 必須の事前検査のうち未充足: CI gate（branch の push が要る）、24h 以内の backup、replay evidence、所有者の承認
   （`approval_ref`・`enables: []` が必須になった）。
3. 今回の指示は「必要な承認が確認された場合のみ」。今回の承認はまだ無い。

## 5. 本番の安全性（監査の要点）

| 重さ | 内容 | 状態 |
|---|---|---|
| MAJOR | API（:8000）・Temporal（:7233）・Temporal UI（:8233）・Postgres（:5432）・MinIO（:9000/9001）が 0.0.0.0 に公開。LAN から Temporal に直接 workflow を起動でき、API 認証を迂回できる | 所有者判断（127.0.0.1 へ bind） |
| MAJOR | 保留の無い `blocked` Episode 15 件。認証の無い `POST /resume` が受け付け、11/01 の上限リセット後に承認なしで最大 60 USD を使いうる | 所有者判断（cancel か保留化）。API 認証のデプロイでも塞がる |
| MAJOR（運用） | 保留中 2 件のため日次は `SKIP_BACKLOG`。11/01 以降も所有者が `provider-credits.py resume` するまで止まる | 所有者判断 |
| 情報 | 10 月の fal 計上 59.36 / 60 USD。日次は上限で止まり、有料呼び出しは起きていない（ガードは正常） | — |
| 情報 | 本番アプリの DB ロールは superuser の `avp`（ADR-0055 の `avp_app` は未適用）。TRUNCATE 防止は 0020 へ延期 | 後続 |

YouTube 投稿は private のみ（直近 48f4eddb の受領で確認）。未照合の予約 0。OpenSearch は停止を維持。

## 6. 機能ごとの有効化条件（すべて所有者の別ゲート）

- `AVP_QUALITY_GATE_ENFORCE`: 0019 適用、API 認証の運用開始、compose で flag を worker / API へ渡す（R3）、
  有効化前に描画済みの Episode の扱いを決める（使用宣言が無く REVIEW のまま。R4）。
- `BGM_ENABLED`: ENFORCE が先、`AVP_UPLOAD_DESCRIPTION_COMPOSED` も ON（OFF だと worker が起動しない）、
  所有者がライセンス証拠つきの実曲を登録、実曲での発話マージンの再校正、所有者の試聴、ADR-0077 / 0083 の承認。
- `GEO_MAPS_ENABLED`: ENFORCE が先（worker が起動時に検査）、所有者が curated gazetteer・各 Episode の依頼と根拠を用意、
  CJK フォントの配置、compose への flag 追加、ADR-0084 の承認。地図の場面も fal 動画は生成される（節約は後続）。
- AI 品質レビュー: 未実装（ADR-0085 は設計）。Agent Runtime の統合と、有料 model は所有者本人の承認が先。

## 7. 未完成の作業

1. R3: compose が `BGM_ENABLED` 以外の flag を渡さない。preflight は `.env` しか読まない。
2. R4: ENFORCE 前に描画済みの動画の再宣言（再描画の要否）。
3. migration 0020（TRUNCATE 防止。ADR-0081 が本番に入ってから）。
4. 手動承認を workflow へ運ぶ（m6）、承認者 id が定数（m5）、`quality_reports` の上限（m9）、ガードが `.env` を読まない（m8）。
5. 地図依頼の自動生成（storyboard から）、地図場面の fal 省略、歴史的海岸線。
6. BGM カタログの MinIO / DB 化、長尺での発話マージン測定時間（R6）。
7. Agent Runtime の統合と AI レビューの実装（ADR-0085）。

## 8. 追加コスト

今回の作業による API 課金: 0（fal・YouTube・LLM の呼び出しなし）。検証はすべて本番外・決定論。
