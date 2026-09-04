# ADR-0001: Temporalを採用する

## Status

Accepted (2026-09-04)

## Context

前身 `ai-video-pipeline` では、工程の順序・retry・再開を全て自前で書いていた。
`automation.py`（約4,700行）が状態機械を回し、`runner.py`（約7,400行）が
各工程を実行し、SQLiteの `jobs` テーブルをlease付きでpollしていた。

実際に起きた問題:

- 1工程が詰まると状態機械が進まず、工場全体が停止した
- 途中再開の経路が工程ごとに手書きされ、互いに食い違った
- プロセスがクラッシュすると、進行中の有料provider呼び出しの照合が失われた
- 「次に何をするか」の知識がworkerとautomationの両方に散り、
  片方だけ更新する事故が繰り返された

必要なのは、**durable execution**（プロセスが死んでも実行状態が残る）と、
**順序決定の単一の置き場所**である。

## Decision

**Temporal OSS を採用し、workflow実行の責務を全て委ねる。**
工程の順序・retry・timeout・signal待ち・補償は Temporal workflow に表現し、
アプリケーション側に独自のスケジューリングループを持たない（INV-5）。
workerは「次に何をするか」を知らない（INV-4）。

## Alternatives

**(a) 自前の状態機械を書き直す** — 前身repoの延長。学習コストゼロだが、
durable executionを自作することになり、それは今回失敗した部分そのもの。却下。

**(b) Celery + Redis** — 導入は軽い。しかしタスクキューであってworkflowエンジンではなく、
多段の依存・signal待ち・長時間timerを表現すると結局自前の状態機械に戻る。
「成熟を7日待ってから実績回収」のような長時間待機が特に苦手。却下。

**(c) Apache Airflow / Prefect** — DAGスケジューラとして成熟。しかしバッチ指向で、
「1本の動画ごとに独立した長期実行インスタンスが、人間のsignalを待つ」という
形が不自然になる。Episodeごとに動的DAGを作る運用は破綻しやすい。却下。

**(d) AWS Step Functions** — durable executionは得られるが、
ローカル開発・自己ホストができずDocker Composeで完結しない。ベンダーロックも重い。却下。

## Consequences

**良い側**
- workerプロセスの死がworkflowを失わせない。再スケジュールされる
- retry方針が宣言的になり、1箇所に集まる
- 長時間待機（成熟待ち、人間の承認待ち）が自然に書ける
- 工程追加時に触るのがworkflow定義1箇所になる

**悪い側 / 引き受けた負債**
- **運用対象が1つ増える**（Temporal server + そのDB）。Docker Composeが重くなる
- workflowコードに**決定性の制約**がかかる。`datetime.now()` や乱数、
  ネットワークI/Oをworkflow内に書けない。この制約を知らないエージェントが
  素朴に書くと本番でのみ壊れる → architecture testで検出する必要がある
- workflow定義の**バージョニング**が新しい難所になる。実行中のworkflowがある状態で
  定義を変えると壊れうる。Temporalのversioning APIを使う運用ルールが要る
- Temporalの内部状態をdomain stateと混同しやすい。明示的に禁止した（INV-8）
- 学習コストが実在する。AGENTS.mdとADRで補うが、ゼロにはならない
