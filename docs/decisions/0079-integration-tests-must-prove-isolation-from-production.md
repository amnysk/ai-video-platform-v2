# ADR-0079: integration テストは本番スタックから隔離されていると**積極的に宣言**できるときだけ走る

## Status

Accepted（v3 Phase 3 / Sonnet A。2026-10-29。テスト基盤の変更のみ。本番の挙動・DB・設定は変えない）

## Context

本番 compose（project `avp2`）はこのホストで Temporal（`localhost:7233`、namespace `default` と `avp-test`）、
PostgreSQL（`localhost:5432`、DB `avp` と `avp_test`）、MinIO（`localhost:9000`）、API（`0.0.0.0:8000`）を
host ポートに公開している。実測（2026-10-29、読み取りのみ）:

1. `tests/integration/test_pipeline_schedule.py:48` が `TEMPORAL_ADDRESS = "localhost:7233"` を直書きしていた。
   `pytest tests/integration` を本番と同じホストで直接実行すると、**本番 Temporal サーバ**に繋がる。
2. 既存の guard（`tests/support/temporal.py` の namespace、`minio.py` の bucket、`infrastructure/db/safety.py` の
   `*_test` + DB コメントの印）は、**本番サーバの中の別の名前空間**しか区別しない。`avp-test` namespace・
   `artifacts-test` bucket・`avp_test` DB は本番サーバの上に実在する（PostgreSQL の `pg_database` に `avp_test` が
   在る）ので、guard を全部通った状態でも本番の Temporal / PostgreSQL / MinIO に書き込む。
   過去に MinIO の `artifacts` bucket が約834件のテスト由来オブジェクトで汚染された事故がある（minio.py）。
3. guard は「環境変数が正しく設定されていること」を前提にしている。設定の欠落や綴り違いでは何も止まらない。

## Decision

### 1. 拒否リストではなく、隔離の**積極的な宣言**を要求する（fail closed）

integration の収集時（`tests/integration/conftest.py`、test module の import より前）に、接続先
（`TEMPORAL_ADDRESS` / `TEST_DATABASE_URL` / `DATABASE_URL` / `MINIO_ENDPOINT`）が1つでも設定されていれば、
`AVP_INTEGRATION_ISOLATION` が次の**完全一致**のどれかであることを要求し、違えば skip ではなく**例外**:

| 宣言 | 誰が設定するか | 追加で要求すること |
|---|---|---|
| `apptest:<project>` | `deploy/logging/test/compose.apptest.yaml`（run-e2e.sh の隔離スタック） | project は `avp2-oslog-c*`（本番 `avp2` は不可）。接続先は内部サービス名 `temporal` / `postgres` / `minio` だけ。loopback・このホストの IP に解決される名前は拒否 |
| `ci-ephemeral` | `.github/workflows/ci.yml` の integration / alembic job | `GITHUB_ACTIONS=true` が同時に要る。接続先は loopback だけ |

宣言が無い・綴りが違う・空・前後に空白、いずれも拒否する。**手で設定しない**（本番と同じホストで
`ci-ephemeral` を手で設定しても `GITHUB_ACTIONS` が無いので拒否される）。

### 2. 宣言が嘘でも動く動的検査（Temporal）

接続先の Temporal が到達可能なら、`default` namespace に日次 Schedule `avp-daily-episode`（`contracts.pipeline.
DAILY_SCHEDULE_ID`）が在るかを **describe だけ**で確かめる。在れば本番として拒否。NOT_FOUND なら通す。
NOT_FOUND 以外の失敗は判定不能として拒否（fail closed）。到達不能は何もしない（テストが各自 skip する）。
PostgreSQL / MinIO には本番と隔離スタックを区別できる読み取りだけの指標が無い
（隔離スタックも `avp` / `artifacts` を持つ）ので、静的な宣言と接続先の規則に依存する。これは限界として明記する。

### 3. 接続先の直書きを禁止する

`tests/integration/` と `tests/support/` は接続先の住所を直書きしない（`localhost:7233` 等、
`TEMPORAL_ADDRESS = "..."`、`connect_test_client("...")`、DB URL の host）。`test_pipeline_schedule.py` は
`tests/support/temporal.temporal_test_address()` を使い、未設定なら skip する。

## Consequences

- 本番と同じホストで `pytest tests/integration`（や `make test-integration`）を直接実行すると、収集時に例外で止まる。
  integration は `deploy/logging/test/run-e2e.sh`（隔離スタック）か CI で実行する。これは意図した変更。
- 既存の CI job は `AVP_INTEGRATION_ISOLATION: ci-ephemeral` を足した（env 1行）。他の挙動は変わらない。
- 本番ホスト上の `avp_test` DB / `avp-test` namespace / `artifacts-test` bucket は、テストが使わなくなるので
  不要になる可能性がある。削除は所有者の判断（本 ADR は削除しない）。
- 残るリスク: 悪意または不注意で `ci-ephemeral` と `GITHUB_ACTIONS=true` の両方を手で設定し、かつ PostgreSQL /
  MinIO だけを本番に向ける場合。Temporal は動的検査で止まるが、PostgreSQL / MinIO は止まらない。
  対策は別インスタンスへの分離（docs/operations/db-role-separation.md）で、本 ADR の範囲外。

## Alternatives

- 拒否リスト（`localhost:7233` / `5432` / `9000` を弾く）だけにする: CI は同じ loopback の使い捨てサービスを使うので
  例外が要り、綴り違い・別の LAN IP・別名で抜ける。許可リスト（宣言）にした。
- 環境変数 `CI=true` を CI の印にする: 開発者の端末でも設定されうる。`GITHUB_ACTIONS=true` と `ci-ephemeral` の両方を要求する。
- PostgreSQL / MinIO にも動的な本番判定を足す: 隔離スタックも同じ DB 名・bucket 名を持つので、読み取りだけで区別できる
  指標が無い。別インスタンスへの分離（db-role-separation.md）が正しい解で、本 ADR では扱わない。

## 影響を受ける不変条件

INV-121（宣言と接続先の規則）、INV-122（直書き禁止と動的検査）。
