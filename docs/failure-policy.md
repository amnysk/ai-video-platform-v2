# 失敗ポリシー

前身repoで一番多かった停止原因は「失敗の種類を区別しなかったこと」だった。
どの失敗も同じように扱った結果、(a) retryすれば済むものを人間待ちにし、
(b) 人間の判断が要るものを自動で再試行して課金と時間を溶かした。

## 1. 失敗クラス

すべてのJob失敗は、**Activityが送出する例外の型**で次のいずれかに分類される。
散文のgrepや文字列マッチで分類しない。

| クラス | 意味 | Temporalの扱い | Episodeへの影響 |
|---|---|---|---|
| `transient` | 一時障害（ネットワーク、5xx、rate limit） | 自動retry（指数backoff） | なし |
| `retryable` | 再実行で解決しうる（provider側の生成失敗、品質ゲート未達） | 上限付きretry / 再生成 | `needs_work` へ。terminalにしない |
| `needs_input` | 人間の判断が要る（予算超過、権利判定、分類不能） | retryせずsignal待ち | `blocked` へ。terminalにしない |
| `permanent` | **決定論的な**入力自体が不正で再実行しても同じ（存在しない入力Artifact、未知の schema_version） | retryしない | `failed`（terminal） |

**分類できない例外は `needs_input` として扱う**（INV-12）。
`permanent` は「同じ入力で必ず同じ失敗になる」と示せる場合だけ。

### 非決定的な生成器（LLM）の出力欠陥（ADR-0014）

**LLM は同じ入力でも違う出力を返す**ので、出力の形式不正に `permanent` の条件
（「同じ入力で必ず同じ失敗になる」）は成立しない。したがって:

| 事象 | クラス |
|---|---|
| 出力が invalid JSON（截断・前置き混入） | `retryable` |
| パースできたがスキーマ違反 | `retryable` |
| ナレーションがシーン尺の読み上げ予算を超える（`ScriptNarrationOverBudgetError`、ADR-0026 追補） | `retryable`（次ラウンドで再生成。`max_attempts` を使い切ったら Job は失敗し、Episode は `RETRY_BUDGET_EXHAUSTED` で `blocked`。下の `needs_input` への昇格は**未実装**） |
| 同じ `input_hash` で規定ラウンド連続して同種の違反 | `needs_input`（プロンプトとスキーマの不整合。人間が直す） |
| 入力Artifactが存在しない / 未知の schema_version | `permanent` |

**出力の修復（截断JSONの補完など）はしない。** 推測して読まない（artifact.md）。

**実装との差（既知の負債、ADR-0026 追補）**: 「同種の違反が続いたら `needs_input`」の昇格は台本・storyboard
工程とも実装されていない。現実の挙動は、ラウンドを `max_attempts` まで使い切ると `RETRY_BUDGET_EXHAUSTED`
で Episode が `blocked` になる（`workers/planning/activities.py` / `workers/storyboard/activities.py` の
`record_failure`）。

### Storyboard 工程（ADR-0015 / ADR-0016）

| 例外 | クラス | 事象 |
|---|---|---|
| `StoryboardOutputUnparseableError` | `retryable` | 生成出力が JSON として読めない |
| `StoryboardSchemaViolationError` | `retryable` | 固定 schema 違反 / 時間軸が正規化の許容を超える / 台本のカバレッジ不足 |
| `StoryboardNarrationSpanTooShortError` | `retryable` | 台本シーンに割り当てた区間がナレーションの読み上げ予算に足りない（ADR-0026 追補。`StoryboardSchemaViolationError` の下位） |
| `StoryboardInputMissingError` | `needs_input` | 現行の台本 Artifact が無い |
| `StoryboardInputInvalidError` | `needs_input` | 保存された台本が読めない / sha256 不一致 |
| `GenerationSpecUnavailableError` | `needs_input` | 固定した OpenMontage commit / blob が読めない |
| `WorkspaceUnavailableError` | `retryable` | 一時作業領域を安全に用意・削除できない |

入力台本が無い・壊れている場合を上表の「入力Artifactが存在しない → `permanent`」に落とさないのは、
台本工程を人間が再実行すれば回復する（回復経路がある）ため。検査:
`tests/unit/test_failure_class_registry.py::test_storyboard_exceptions_classify_by_their_base`。

### Production 工程（ADR-0017）

| 例外 | クラス | 事象 |
|---|---|---|
| `ProviderSubmitAmbiguousError` | `needs_input` | 有料ジョブの submit が戻らず provider job 参照を記録できなかった（呼んだか不明） |
| `UnreconciledReservationError` | `needs_input` | `dispatched_at` ありで provider job 参照も evidence も無い予約が残っている |
| `ProviderRejectedError` | `needs_input` | provider が依頼を拒否（コンテンツポリシー等）。人間がプロンプト・素材を直す |
| `ProviderJobFailedError` | `retryable` | provider 側ジョブの失敗。次ラウンド（新しい予約）で再生成 |
| `ProviderPollDeadlineError` | `retryable` | 完了待ちの期限切れ。ジョブの状態は不明なので**同じ予約で再 await**（再送しない。上限を使い切ったら記録して止まる） |
| `MediaValidationError` | `retryable` | 生成メディアが形式・解像度・尺の規則を満たさない |
| `ProductionInputMissingError` | `needs_input` | 現行の storyboard / 台本 / シーン画像が無い |
| `ProductionInputInvalidError` | `needs_input` | 入力 Artifact が読めない・sha256 不一致・相互に食い違う |
| `VoiceLanguageUnsupportedError` | `needs_input` | 設定した音声モデルが台本の言語を話せない（音声モデルか台本の言語を人間が直す） |

有料の submit Activity は `maximum_attempts=1`、await Activity は provider job 参照に対して冪等なので
retry してよい（最大5回）。ただし await で**ラウンドが確定済み**の失敗（`ProviderJobFailedError` /
`MediaValidationError`）は Activity の retry を止める（`non_retryable=True`）。型名は変えないので workflow は
`retryable` と分類して新しいラウンドへ進む（ADR-0017 §4）。

workflow 側で await が失敗したとき（ADR-0017 §4）:

| await の失敗 | workflow の動作 |
|---|---|
| `ProviderJobFailedError` / `MediaValidationError`（ジョブが確定的に終わった） | 試行予算内なら新しい submit（台帳が実効ラウンドを決める） |
| `ProviderPollDeadlineError` / `ProviderTimeoutError` / `ProviderInvocationError` / transient / Activity timeout（状態不明） | **同じ予約で** await を追加実行（既定3回）。使い切ったら記録して止まる。新しい submit はしない |
| それ以外 | 失敗クラスのまま記録 |

workflow の cancel は `needs_input`（`blocked`）として記録する。POST で再開できる（ADR-0017 §8）。

Activity 境界の写像（画像・音声・動画共通、`infrastructure/production/activity_errors.py`）:

| 例外 | Activity の扱い |
|---|---|
| ドメイン例外 | `ApplicationError(type=<型名>)`。`needs_input` / `permanent` は `non_retryable` |
| DB の接続断・操作エラー / オブジェクトストアの通信失敗・5xx / 作業領域の `OSError` | `TransientError`（入力の読み取り中でも入力不正にしない） |
| `InvalidTransitionError` / `ArtifactConflictError` | `needs_input`（台帳・Artifact の食い違い） |
| `UnreconciledReservationError`（`dispatched_at` の二重書き込み、poll 時に参照が読めない・ホスト外） | `needs_input` |
| 未分類 | そのまま（Temporal の retry と型名分類 / INV-12） |

検査: `tests/unit/test_activity_errors.py`。ローカル非課金の音声合成は台帳に載せず Temporal の retry（最大3回）に委ねる
（INV-15 の限定例外、ADR-0017）。検査:
`tests/unit/test_failure_class_registry.py::test_production_exceptions_classify_by_their_base`。

### Render 工程（ADR-0019）

| 例外 | クラス | 事象 |
|---|---|---|
| `RenderInputMissingError` | `needs_input` | 現行の production_manifest / 参照 Artifact が PG か MinIO に無い |
| `RenderInputStaleError` | `needs_input` | マニフェストが指す Artifact が現行でない |
| `RenderInputIntegrityError` | `permanent` | 入力 Artifact の sha256 不一致・契約違反 |
| `RenderSourceMediaError` | `needs_input` | 素材メディアが読めない・未対応形式・尺の食い違い |
| `DurationReconciliationError` | `needs_input` | シーン動画の尺を storyboard の尺へ合わせられない（凍結の上限超過） |
| `VoiceTimelineOverflowError` | `needs_input` | ナレーション音声が次の音声と重なる / 総尺を許容以上に超える |
| `RenderEngineFailedError` | `retryable` | 描画エンジンの非zero終了・シグナル終了 |
| `RenderEngineTimeoutError` | `retryable` | 描画エンジンの時間切れ（`RenderEngineFailedError` の下位型） |
| `RenderWorkspaceFullError` | `retryable` | 作業領域の空き不足（事前検査 / ENOSPC）。自動削除しない |
| `RenderEngineUnavailableError` | `needs_input` | バイナリ・フォントが無い / 固定 sha256 と不一致（運用者が導入し直せば回復） |
| `FinalVideoValidationError` | `needs_input` | 完成動画の決定的な技術検査不合格（解像度・codec・音声欠落など） |
| `FinalVideoCorruptError` | `retryable` | 完成動画がデコードできない / 読み戻し sha256 不一致 |
| `UnknownRenderProfileError` | `needs_input` | 未登録の render profile id（API でも先に弾く） |

描画 Activity は retry 最大3回（retryable の型だけ）。使い切ったら `blocked`（`RETRY_BUDGET_EXHAUSTED`）で terminal にしない。
workflow の cancel は production と同じく `needs_input`（`blocked`）として記録する（子プロセスを止め、作業領域を片付けてから。terminal にせず POST で再開できる）。MinIO / DB の通信失敗（S3 の 5xx・流量制限を含む）は `transient`。
検査: `tests/unit/test_render_vocabulary.py::test_render_exceptions_classify_by_their_base`。

### Upload 工程（ADR-0020）

| 例外 | クラス | 事象 |
|---|---|---|
| `UploadInputMissingError` | `needs_input` | 現行の final_video / 台本の参照か本体が無い |
| `UploadIntegrityError` | `permanent` | final_video の sha256 がメタデータ・契約と不一致（YouTube を呼ばない） |
| `UploadsPausedError` | `needs_input` | `UPLOADS_PAUSED` / operational switch `uploads_paused` が有効（session 開始前と送信中に止める。session は残る） |
| `UploadAuthError` | `needs_input` | OAuth の `invalid_grant` / 権限不足 / チャンネル未開設（401 は1度 refresh してから） |
| `UploadQuotaExceededError` | `retryable` | quotaExceeded / uploadLimitExceeded / rateLimitExceeded（長い backoff） |
| `UploadRejectedError` | `needs_input` | YouTube がメタデータ・動画を拒否（invalidTitle 等） |
| `UploadOutcomeUnknownError` | `needs_input` | bytes 送信後に結果が読めず、マーカー照合でも見つからない。**新しい session を開かない** |
| `UploadProcessingPendingError` | `retryable` | 受理済みだが YouTube の処理中 / まだ見えない（ADR-0022。30 秒〜10 分間隔、6 時間で `blocked`） |
| `UploadProcessingFailedError` | `needs_input` | YouTube が拒否・処理失敗・削除、またはチャンネル違い・private でない（ADR-0022。再投稿しない） |

投稿 Activity は retry 最大3回（retryable の型だけ）。使い切ったら `blocked`。自動で開く予約ラウンドは1つだけで、
同じ upload key で投稿し直すのは運用者の `OPERATOR_ABANDONED` の後だけ（ADR-0020 §8）。cancel は送信を止め、
session を残して `blocked`。検査（契約）: `tests/contract/test_upload_contracts.py`。

### Topic Planner（ADR-0025）

Planner は Episode を作る**前**に走るので、失敗は Episode の状態ではなく Daily workflow の結果に現れる。

| 事象 | 扱い |
|---|---|
| Analytics の live 取得失敗（通信・401/403・scope 未付与・quota） | **致命的でない**。`normal` → 最新 snapshot で `stale_analytics` → snapshot も無ければ `no_analytics` と劣化して続ける。mode は `topic_plans` に記録 |
| LLM 出力の invalid JSON / `TopicCandidate` 契約違反 | `retryable`（ADR-0014）。Activity は `maximum_attempts=1`、retry は workflow のラウンド（`policy.max_rounds`） |
| ラウンド内の全候補が hard duplicate / cooldown 内 | 次ラウンド（落ちた subject を `avoid_subjects` で避けさせる） |
| 全ラウンドを使い切った | non-retryable（`needs_input` クラス）で Planner が失敗 → **Daily も失敗し、Episode を作らない**（INV-21） |
| DB の失敗（接続断・操作エラー） | `transient` として Temporal が retry。**握りつぶさない**（Analytics の fallback に混ぜない。snapshot / Memory が読めないまま企画しない） |
| 一意制約の競合（同じ日・profile の Plan が既にある） | 失敗ではない。既存 Plan を返す（INV-22） |

## 2. Episodeをterminal failedにしてよい条件

次の全てを満たすときだけ `failed`：

1. 失敗が `permanent` に分類されている
2. その工程に代替経路が無い
3. 人間の入力で回復しうる余地が無い

それ以外は `needs_work` / `blocked` に留め、**Episodeは生き続ける**。

## 3. 部分失敗の封じ込め

- Job失敗はそのEpisodeのworkflowにのみ影響する（INV-13）
- 1つのproviderのダウンは、そのproviderを使うActivityだけを止める
- workerプロセスの死は進行中のworkflowを失わない（Temporalが再スケジュール）
- どのJobも、直前に成功したArtifactから再開できる（下記）

## 4. 途中再開

再開の単位は **Artifact** であって工程ではない。

- 各Activityは開始時に「必要な入力Artifactが揃っているか」を確認する
- 出力Artifactが既に存在し、同じ入力hashから作られているならskipして返す
  （= Activityの冪等性、INV-17）
- したがって workflow を最初から再実行しても、済んだ工程は課金を伴わずに素通りする

これが「途中再開」の唯一の実装方法である。工程ごとの再開フラグを作らない。

## 5. retry予算

- `transient`: Temporal の RetryPolicy に委ねる（回数上限あり、無限retry禁止）。
  **ただし課金を伴う外部呼び出しの Activity は例外**（ADR-0013）:
  `maximum_attempts=1` とし、retry は workflow のラウンドとして予約台帳を通す。
  Temporal の自動retryに委ねると、課金呼び出しが台帳を経ずに増える
- `retryable`: Episodeごとに**課金を伴う再生成の上限**を持つ。上限に達したら `blocked`
- retryラウンドは1日の制作枠を消費しない（枠は「開始したEpisode」を数える）

## 6. 停止検知

- 一定時間 `running` のまま進まないEpisodeは stalled として可視化する
- stalled は自動修復の対象ではなく、まず**通報**する
- 自動修復経路の無い失敗クラスを自動ルーティング表に載せない

## 7. 全体停止スイッチ

- `PAUSED`: 新規workflowの起動を止める（実行中は完走させる）
- `UPLOADS_PAUSED`: 投稿Activityだけを止める。スコープが違うので別スイッチ。session を開始する前に読み、
  有効なら `UploadsPausedError`（`needs_input`）で YouTube を呼ばずに止める（ADR-0020）

両方を独立に読む。片方でもう片方を代用しない。
