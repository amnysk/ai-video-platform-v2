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
| 同じ `input_hash` で規定ラウンド連続して同種の違反 | `needs_input`（プロンプトとスキーマの不整合。人間が直す） |
| 入力Artifactが存在しない / 未知の schema_version | `permanent` |

**出力の修復（截断JSONの補完など）はしない。** 推測して読まない（artifact.md）。

### Storyboard 工程（ADR-0015 / ADR-0016）

| 例外 | クラス | 事象 |
|---|---|---|
| `StoryboardOutputUnparseableError` | `retryable` | 生成出力が JSON として読めない |
| `StoryboardSchemaViolationError` | `retryable` | 固定 schema 違反 / 時間軸が正規化の許容を超える / 台本のカバレッジ不足 |
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
- `UPLOADS_PAUSED`: 投稿Activityだけを止める。スコープが違うので別スイッチ

両方を独立に読む。片方でもう片方を代用しない。
