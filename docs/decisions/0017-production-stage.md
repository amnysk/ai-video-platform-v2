# ADR-0017: Production 工程（画像・音声・動画）と Episode `assets_ready`

## Status

Accepted (2026-09-13)

## Context

Phase 4 は storyboard（ADR-0015）からシーン素材を作る: storyboard シーンごとの静止画と動画、
台本シーンごとのナレーション音声。Phase 3 までと違い、

- 1 Episode あたりの外部呼び出しが**シーン数 × メディア種別**に増える（数十回）
- 画像・動画の有料 provider は**非同期ジョブ型**（submit → 数十秒〜数十分待つ → 取得）で、
  Codex CLI のように「呼び出しが戻れば出力が手元にある」同期型ではない
- 音声合成はローカルの非課金プロセスで、外部への支払いが無い

ADR-0013 の予約台帳は同期呼び出しを前提に書かれており、「submit が成功したのに結果取得の前に
worker が落ちた」ときの証拠（provider job 参照）を置く列が無い。これを放置すると再開時に
「dispatched だが生出力が無い」＝曖昧として毎回 `blocked` になるか、最悪は再 submit で二重課金する。

ADR-0015 は「Phase 4 の workflow が storyboard の先へ続けて進むなら駐機点を再評価」を負債に残した。
Phase 4 の `ProductionWorkflow` は素材を揃えた時点で終わり、次工程（Phase 5 Render）はまだ存在しない。
Episode は素材の後で**実際に駐機する**ので、ADR-0011 の基準で状態が要る。

ADR-0015 は heartbeat と cancel を「今後の作業」とした。Phase 4 の待機は最長 40 分に及ぶので、
ここで再評価する。

## Decision

**Phase 4 を共通の土台 + 4A Image / 4B Voice / 4C Video に分け、非同期ジョブ型の有料呼び出しは
provider job 参照を台帳へ write-once で commit してから待つ。Episode に駐機点 `assets_ready` を足す。**

### 1. 分割

- 土台（本ADR / ADR-0018）: 語彙・Artifact 契約・ドメイン規則（入力指紋・作業計画・検査規則・マニフェスト）・
  provider 中立の port・永続化・メディアのデコード
- 4A Image / 4B Voice / 4C Video: provider adapter と Activity。互いを import しない（INV-3）
- workflow（`workers/production`）とメディア別 worker は `contracts/production_activities.py` の
  **Activity 名と入出力 dataclass だけ**を共有する。これで並行に実装できる

### 2. 語彙

- `EpisodeStatus.ASSETS_READY`、`JobType.PRODUCE_SCENE_IMAGE / PRODUCE_SCENE_VOICE / PRODUCE_SCENE_VIDEO / ASSEMBLE_PRODUCTION`、
  `ArtifactType.SCENE_IMAGE / SCENE_VOICE / SCENE_VIDEO / PRODUCTION_MANIFEST`、
  `ProviderCall.FAL_IMAGE / FAL_VIDEO`
- `PRODUCTION_WORKFLOW = ("ProductionWorkflow", "production")`、task queue `production-image` /
  `production-voice` / `production-video`
- 遷移: `in_progress --ASSETS_READY--> assets_ready`、`assets_ready --STAGE_ADMITTED--> in_progress`、
  非terminal からの `CANCELLED`（既存の派生規則）。production の入場は `storyboard_ready` と `assets_ready` から

### 3. 非同期ジョブ型の有料呼び出し（INV-15 の実体）

書き込み順序:

1. 同じ `input_hash` の現行 Artifact があれば呼ばずに返す（INV-17）
2. 同じ Episode + provider + **scene** に evidence の無い予約が（自分のキー以外に）あれば止める
3. 予約を INSERT → **commit**（`estimated_cost_usd` を記録。cost_status は「見積もり」）
4. `dispatched_at` → **commit**
5. `submit`
6. **provider job 参照を `record_provider_job_ref` で即座に write-once → commit**
7. await: `poll` を繰り返す（heartbeat。retry 可。**再 submit しない**）
8. `download` → 生の取得物を `provider-raw/` に evidence として保存
9. `mark_spent` → **commit**（検証より前）
10. 検証（`domain/production/media.py`）→ 正規化 → メディア本体と Artifact を保存 → `attach_artifact`

再開の分岐（`idempotency_key` で引いた予約）:

| 予約の状態 | 動作 |
|---|---|
| 行なし / `dispatched_at` NULL | 予約・dispatch・submit へ進む |
| `reserved` + `dispatched_at` + provider job 参照あり | **await を再開**（再 submit しない） |
| `reserved` + `dispatched_at` + 参照なし | `UnreconciledReservationError`（needs_input、人手照合） |
| `spent` + 生の取得物あり | 検証から再開 |
| `spent` + Artifact あり | 既存を返す |

submit が戻らず結果が分からない場合、adapter は `ProviderSubmitAmbiguousError`（needs_input）を投げ、
予約は `reserved` + dispatched + 参照なしのまま残る。

### 4. 実行ポリシー

- submit Activity: `maximum_attempts=1`（INV-15）。retry は workflow のラウンド（新しい予約）
- await Activity: `start_to_close=40分`、`heartbeat_timeout=90秒`、retry 最大5回。
  provider job 参照に対して冪等なので retry が再課金にならない。
  **cancel されたら poll をやめて終わる。provider 側のジョブは cancel しない**（課金は既に発生しうる。
  結果を後で回収できる余地を残す）
- ADR-0015 の heartbeat / cancel の負債は **await Activity に限って実装する**。submit と
  他工程の Activity は短時間で終わるので対象外のまま
- 並行数は task queue ごとに設定（既定 image 2 / voice 1 / video 1、`infrastructure/config.py`）
- seed は provider へ送らない。再現性は `input_hash` による Artifact の再利用で、
  バリエーションはラウンドで得る

### 5. INV-15 の限定例外: ローカル非課金の生成器

INV-15 の対象は**課金を伴う外部呼び出し**である。ローカルで決定論的に動き外部への支払いが無い
音声合成（Piper 等）は予約台帳に載せず、Temporal の retry（最大3回）に委ねる。
二重実行しても課金が増えず、同じ入力から同じ出力が出るので、台帳の目的（二重課金と宙吊りの防止）に
該当しない。**有料の TTS を導入する場合はこの例外に入らない**（台帳に載せる）。

### 6. ドメイン規則（`domain/production/`）

- `identity.py`: `image_input_hash` / `voice_input_hash` / `video_input_hash`。
  ラウンド・試行・job・時刻・run id・seed を含めない。
  `voice_input_hash` は台本側（`script_sha256` / `script_scene_id` / `narration_sha256`）に加えて
  `storyboard_sha256` と整列した `storyboard_scene_ids` を含む。音声 Artifact は `source_storyboard` と
  参照シーンを記録するので、storyboard の再計画で古い音声を再利用しない（ローカル非課金なので再生成の費用は無い）。冪等キーは `domain.script.identity.idempotency_key`
- `planning.py`: storyboard シーンごとに画像+動画、台本シーンごとに音声（参照する storyboard シーン付き）
- `media.py`: 検査規則と 9:16 正規化計画。画像は正規化後 1080x1920 / png|jpeg|webp / 0 < bytes < 25MB、
  音声 200ms..60s / ≥16kHz / 1..2ch、動画は尺 ±max(300ms, 5%) / 9:16 ±1% / 高さ ≥1280 / 20..60fps / フレーム > 0
- `manifest.py`: マニフェストの組み立てとカバレッジ（全 storyboard シーンに画像+動画、全台本シーンに音声）
- `ports.py`: provider 中立の `ImageGenerator` / `VideoGenerator`（非同期ジョブ型）/ `VoiceGenerator`（同期）

### 7. 失敗クラス

`ProviderSubmitAmbiguousError` / `ProviderRejectedError` / `ProductionInputMissingError` /
`ProductionInputInvalidError` は needs_input、`ProviderJobFailedError` / `ProviderPollDeadlineError` /
`MediaValidationError` は retryable（`docs/failure-policy.md`）。
コンテンツポリシー拒否を permanent にしないのは、プロンプトを人間が直せば回復するため。

## Alternatives

**(a) 画像・音声・動画を1つの worker / 1つの ADR で実装する** — 語彙の追加が1回で済む。
しかし provider の性質（非同期有料 / 同期ローカル）が異なり、並行数・タイムアウト・retry が別物になる。
1 worker にすると遅い動画生成が画像の枠を塞ぐ。却下。

**(b) 同期呼び出しとして扱い、submit から download までを1つの Activity にする** — 台帳の変更が要らない。
しかし 40 分の Activity を `maximum_attempts=1` で走らせると、worker の再起動1回で結果が失われ、
provider job 参照を持たないので曖昧（blocked）になる。却下。

**(c) provider job 参照を Temporal の Activity 結果（workflow 履歴）に置く** — DB 列が要らない。
しかし状態の権威は PostgreSQL（INV-8）で、submit の直後・Activity 完了の前に落ちると履歴に残らない。却下。

**(d) Piper も台帳に載せる** — 一様になる。しかし evidence 照合と曖昧時の blocked が、支払いの無い
呼び出しに人手照合を強いる。却下（限定例外として明記する）。

**(e) `assets_ready` を足さず `in_progress` で待つ / `storyboard_ready` に戻す** — ADR-0015 (a) と同じ理由で却下。

**(f) seed を送って再現性を得る** — provider ごとに seed の意味が違い、モデル更新で再現しない。
Artifact 再利用の方が確実。却下。

## Consequences

**良い側**
- worker が落ちても、submit 済みの有料ジョブは provider job 参照から回収でき、再 submit しない
- 画像・音声・動画を並行に実装・運用できる（task queue と並行数が独立）
- メディアの検査規則がドメインの純粋関数で、provider を差し替えても基準が揺れない

**悪い側 / 引き受けた負債**
- **ADR-0011 の「前例」がさらに増えた**（`script_ready` / `storyboard_ready` / `assets_ready`）。
  Phase 5 で Render が production に続けて走るなら3つの駐機点の畳み込みを再評価する
- `estimated_cost_usd` は見積もりで、請求額との照合機構は無い（予算上限の強制も未実装）
- await を cancel しても provider 側のジョブは走り続け、課金されうる
- **Cost risk**: 画像・動画の `input_hash` は storyboard 全体の sha256 を含むので、storyboard の1シーンを
  直しただけでも全シーンの有料素材（画像・動画）が再生成対象になる（課金）。シーン単位の指紋への縮小は
  負債として受け入れる（音声も storyboard sha を含むが非課金）
- `assets_ready` からの再実行が失敗したときの状態と現行 Artifact の食い違いは ADR-0015 と同じ負債
- provider job 参照の保持期間は provider 依存で、長時間の `blocked` 後に回収できない場合がある
  （その予約は人手照合で `spent` にする）

## 陳腐化条件

- 有料の TTS を導入したとき（§5 の例外に入らない）
- Phase 5 Render が production の後に同じ workflow で続くようになったとき（駐機点の再評価）
- provider がジョブ参照ではなく webhook のみを提供するようになったとき（§3 の順序の再設計）
