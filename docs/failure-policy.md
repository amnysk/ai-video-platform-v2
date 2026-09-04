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
| `permanent` | 入力自体が不正で再実行しても同じ（スキーマ違反、存在しない素材） | retryしない | `failed`（terminal） |

**分類できない例外は `needs_input` として扱う**（INV-12）。
`permanent` は「同じ入力で必ず同じ失敗になる」と示せる場合だけ。

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

- `transient`: Temporal の RetryPolicy に委ねる（回数上限あり、無限retry禁止）
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
