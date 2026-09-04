# ADR-0002: PostgreSQLをapplication stateのsource of truthにする

## Status

Accepted (2026-09-04)

## Context

前身repoは SQLite（`data/studio.db`）を使っていた。単一プロセスの間は問題なかったが、
v2では FastAPI・複数のTemporal worker・分析ジョブが同時にstateへ書く。
SQLiteの書き込み単一化（ライタ1本）はこの形に合わない。

また、状態の権威が曖昧だったことが事故の一因だった。同じ真実がDBと
ディスク上のsentinelファイル（`state/PAUSED` 等）とJSON成果物に散り、
後から設計を足したときに片方だけ更新された。

## Decision

**PostgreSQL を application state の唯一の source of truth とする**（INV-7）。
Episode / Job / Artifact参照 / コスト予約 / 冪等キーは全てPostgreSQLに置く。
Temporal・MinIO・UIキャッシュの値を権威として読まない。

## Alternatives

**(a) SQLiteを継続** — 移行コストゼロ。しかし複数worker同時書き込みで
`database is locked` に悩まされるのが目に見えている。
`SELECT ... FOR UPDATE` が無く、Jobの排他や冪等キーの実装が弱い。却下。

**(b) MongoDB等のドキュメントDB** — Artifactメタが半構造的なので相性はある。
しかし本システムの核心は**状態遷移の整合性**であり、トランザクションと
制約（UNIQUE、CHECK、外部キー）が効くことの方が価値が高い。却下。

**(c) Temporalのworkflow state を権威にする** — 一見DRY。しかしworkflow履歴は
クエリ用のストアではなく、「今 `blocked` のEpisodeを一覧する」ような
UIの基本操作ができない。INV-8で明示的に禁止した。却下。

**(d) DynamoDB / Firestore** — ローカル開発とDocker Compose完結の要件に反する。却下。

## Consequences

**良い側**
- 複数workerからの同時書き込みが安全。`FOR UPDATE` と UNIQUE制約が使える
- 冪等キー（INV-14）をDB制約として表現でき、アプリのバグで破れない
- UIに必要な検索・集計（状態別一覧、stalled検知、コスト集計）が素直に書ける
- JSONB があるのでArtifactメタの半構造データも扱える

**悪い側 / 引き受けた負債**
- サーバプロセスが増える。ローカル環境がSQLiteより重い
- **マイグレーション運用**が必須になる（Alembic）。複数エージェントが並行して
  マイグレーションを書くと連番が衝突する → ブランチ規律（AGENTS.md §5）に依存する
- Temporal も自前のPostgreSQLを要する。同一インスタンスに同居させるか
  分けるかの判断が要る（初期は同一インスタンス・別DBとする）
- 「DBが権威」という規律は自動では守られない。INV-7の機械検査は現時点で未実装
