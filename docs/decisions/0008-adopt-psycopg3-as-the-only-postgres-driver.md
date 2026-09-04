# ADR-0008: PostgreSQLドライバを psycopg v3 に一本化する

## Status

Accepted (2026-09-04)

## Context

Phase 1 の実装（commit 104153b）は、アプリを `postgresql+asyncpg://` で動かし、
Alembic だけは同期ドライバで実行していた。同期URLは
`infrastructure/db/migrations/env.py` の
`url.replace("+asyncpg", "").replace("+aiosqlite", "")` で作っていた。

その結果、次の3つが同時に成立していた:

1. **PostgreSQLドライバが実質2本必要**。`postgresql://` の既定DBAPIは psycopg2 だが、
   その依存は宣言されていなかった → `docker compose --profile core up` が
   `migrate` コンテナで `ModuleNotFoundError: psycopg2` を出して必ず落ちた
2. **「どの同期ドライバに落ちるか」がコードにも設計書にも書かれていなかった。**
   SQLAlchemy の内部既定に暗黙依存していたため、落ちた理由が読んで分からなかった
3. **置換がブラックリスト方式**。新しい非同期ドライバを足した人がこの1行を
   更新し忘れると、差分に現れない側（＝Alembic）が壊れる。
   これはこのプロジェクトが繰り返し踏んできた事故の形そのもの

Codex が `psycopg2-binary` を依存に追加して (1) を解消した。修正は正しく、
実バグを直している。しかし (2)(3) は残り、ドライバは2本のままだった。

## Decision

**PostgreSQLドライバを `psycopg[binary]` (v3) の1本に統一する。**
asyncpg と psycopg2-binary を依存から外す。接続URLは
アプリも Alembic も同じ `postgresql+psycopg://` を使う。

psycopg v3 は同一ドライバで sync / async の両方を提供するため、
**PostgreSQL では同期/非同期のURL変換が不要になる**。

残る変換（SQLite の `+aiosqlite` → `+pysqlite`）は
`infrastructure/db/urls.py` の `SYNC_DRIVER_BY_ASYNC_DRIVER` に
**ホワイトリストとして1箇所だけ**置く。未知の非同期ドライバは黙って通さず、
そのまま返して接続時に明示的に失敗させる。

## Alternatives

**(a) psycopg2-binary を追加したまま維持（Codexの状態）** — 変更ゼロで動く。
しかしドライバ2本・暗黙の既定依存・ブラックリスト置換がすべて残る。
psycopg2 は上流が保守モードで、作者自身が新規採用に psycopg3 を勧めている。
新規システムで保守モードのライブラリを基準点に固定するのは筋が悪い。却下。

**(b) Alembic を async 化して asyncpg 1本にする** — ドライバは1本になり、
「1つの真実」も満たす。しかし `env.py` を `connection.run_sync` 方式へ書き換える
必要があり、Alembic の async テンプレートは同期版より読み手が少なく壊しやすい。
また Phase 2 で「psql で直接繋いで調べる」「同期スクリプトを書く」といった
運用上の要求が出たときに同期ドライバが無い状態になる。
**変更量は (b) の方が大きく、得られる性質は同じ**なので却下。

**(c) 環境変数を2本にする（`DATABASE_URL` + `ALEMBIC_DATABASE_URL`）** —
変換ロジックが消える。しかし同じ接続先が compose / CI / `.env.example` /
設計書の4箇所に二重に書かれ、片方だけ更新する事故を新設する。却下。

**(d) Phase 2 へ延期** — 今は動いているので触らない、という選択。
しかし延期するほど `DATABASE_URL` の読み手は増える（Phase 2 で worker と
リポジトリが増える）。**今の変更点は5ファイル**で、Phase 2 以降は一桁増える見込み。
逆方向（psycopg v3 → asyncpg 復帰）のコストは低いままなので、
判断の非対称性から「今やる」が有利。却下。

## Consequences

**良い側**
- ランタイムのPostgreSQLドライバが 2本 → **1本**
- PostgreSQL のURL変換が消えた。`env.py` は `sync_database_url()` を呼ぶだけ
- 変換の残りはホワイトリスト1箇所（`infrastructure/db/urls.py`）に集約
- 実PostgreSQLに対する Alembic のテストを新設したので、同期ドライバの欠落は
  今後 docker を立てずとも検出される
  （`tests/integration/test_migration_against_postgres.py`）

**悪い側 / 引き受けた負債**
- **asyncpg より遅い可能性がある。** psycopg v3 は C 実装（`psycopg[binary]`）でも
  ベンチマーク上 asyncpg に劣る場面がある。Phase 1 の負荷では計測できる差ではないが、
  Phase 2 でエピソード並列度が上がったときに再評価が要る
- `psycopg[binary]` は libpq を同梱する wheel であり、他の TLS 利用ライブラリとの
  シンボル衝突が理論上ありうる。本番でソースビルド（`psycopg[c]`）へ切り替える
  判断が将来必要になりうる
- SQLite 用の `+aiosqlite` → `+pysqlite` 写像は残った。**変換をゼロにはできていない**

## 陳腐化条件

- 新しい非同期ドライバを追加するとき → `SYNC_DRIVER_BY_ASYNC_DRIVER` を更新する
- psycopg v3 の性能が Phase 2 で問題になったとき → 本ADRを見直す
