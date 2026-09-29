# ADR-0037: Research の依頼・外部呼び出し台帳・成果物を本番の表から分離して永続化する

## Status

Accepted (2026-09-29)

範囲: この ADR は Research Tier B の土台（B1: 契約と永続化）を決める。Gateway・実行器（B2）と
Worker（B3）の決定は §6〜§8 に方針だけを書き、その段で同じ ADR に追記する。Evidence（B4）は
ADR-0038、Trend（B5）は ADR-0039 に分ける。

## Context

ADR-0036 で調査用の検索・本文取得（Port と Adapter）だけを本番系統に取り込んだ。呼び出し元
（依頼の受け付け・実行・結果の保存）は `claude/research`（tip `8cede47`）にあるが、そのままでは
取り込めない。

旧ブランチの永続化（旧 ADR-0031 §4、旧 migration 0011）は **owner-XOR** だった。
`jobs` / `artifact_metadata` / `provider_reservations` の `episode_id` を NULL 可にし、
`research_request_id` を足して「Episode か ResearchRequest のどちらか一方」を CHECK で強制する。
Research の呼び出し件数上限も `provider_reservations.call_seq` に載せていた。本番系統（f209e7c）の
上でこれを再現すると、次のものを書き換えることになる。

- `infrastructure/db/repositories.py`（Job / Artifact / 予約の全メソッドが owner を受け取る形になる）と
  `infrastructure/production/paid_job.py`（`_episode_id_of` の guard を 5 箇所に入れる）。
- `ProviderReservation.episode_id` の型が `str | None` に広がる。本番の読み手は約 37 箇所あり、
  その中に `paid_job.py` の `ProviderRejectionRepository.record(episode_id=spent.episode_id)` がある。
  `provider_rejections.episode_id` は NOT NULL なので、型の narrowing を 1 箇所でも漏らすと実行時に落ちる。
- 本番の CHECK 5 本（`ck_jobs_type`・`ck_artifact_metadata_type`・`ck_provider_reservations_provider`・
  `ck_provider_auth_incidents_provider`・`ck_provider_rejections_provider`）を張り直すことになり、既存テスト
  `tests/contract/test_migration_frozen_vocabulary.py::test_0014_upgrade_adds_exactly_the_adr_0035_values`
  も変更が要る。

これらは 422 復旧（ADR-0034/0035）と課金安全性（ADR-0013、INV-15、INV-32〜35）の中核にあたる。
Research は補助機能であり、本番を止めてよい理由にはならない。

## Decision

**Research の依頼・外部呼び出し台帳・成果物は、新しい 3 つの表（migration 0015）だけに保存する。
本番の表・本番の語彙・本番のリポジトリと課金コードには 1 行も足さない。**

### 1. 分離（INV-37）

- 新しい表は `research_requests`、`research_calls`、`research_artifacts` の 3 つ。FK は research の表の
  間だけで張る。`research_requests.episode_id` は参照だけで、FK を張らない。Research が Episode の
  寿命を縛らないようにするためであり、Evidence は複数の Episode で共有されうる。
- 語彙は `contracts/research.py` だけで宣言する（`ResearchKind` / `ResearchStatus` / `ResearchCall` /
  `ResearchCallStatus` / `ResearchArtifactType`）。`contracts/states.py` の `JobType` / `ArtifactType` /
  `ProviderCall` には値を足さない。migration 0015 はこれらを literal で凍結し、contracts を import しない。
- ORM は `infrastructure/db/models.py` の末尾に 3 クラスを足す。既存のクラスは変えない。
  `tests/contract/test_migration_matches_models.py` が `Base.metadata` と migration を突き合わせるので、
  ORM はそこに置く必要がある。リポジトリは `infrastructure/db/research_repositories.py` に置く。
  `repositories.py` と `paid_job.py` は変更しない。
- Research の失敗は `domain/research/errors.py` で `domain.errors` の基底クラスを継承して定義する。
  `domain/errors.py` には足さない。

### 2. 依頼と同一性

- `request_id = uuid5(RESEARCH_NAMESPACE, idempotency_key)`（`domain/research/ids.py`）。同じキーで
  再送しても別の ID にならない。同じキーで意味が違う依頼を出すと `ResearchIdempotencyConflictError`
  になり、保存済みの行は変えない。
- `request_hash`（`domain/research/identity.py`）は「同じ意味の依頼か」を判定する唯一の定義で、
  旧実装と同じ意味を持つ。`requester` / `idempotency_key` / `episode_id` / 作成時刻は含めない。
  Trend では `as_of` を UTC 日付に丸めて含める。リポジトリは保存する payload から hash を
  自分で計算するので、payload と hash が食い違うことはない。
- 依頼の `limits` は依頼を受けた時点で JSON として凍結する。実行中に環境変数を読み直さない。

### 3. 状態機械

`queued → running → completed | partial | blocked | failed`、`blocked → queued`（人が直して再開）。
終端は `completed` / `partial` / `failed` の 3 つで、`partial` は合格として扱わない。表は
`domain/research/status.py` にだけ置き、リポジトリは compare-and-set でそれを通す。
`completed` / `partial` にしてよいのは、結果が指す成果物がこの依頼の**現行の**
`research_artifacts` 行として先に記録されている場合だけとする（状態だけが先に進んだ半端な行を作らない）。
`blocked` には `code` を持つ理由を必須とする。

### 4. 外部呼び出し台帳と件数上限（INV-36）

- `research_calls` の 1 行が外部呼び出し 1 件にあたる。枠の単位は種別（`search` / `fetch` / `assess`）で、
  検索エンジンごとではない。Web と YouTube の検索はどちらも `max_searches` の同じ枠を使う。
  旧実装は provider ごとに枠を持っていたので、2 つの検索エンジンで上限の 2 倍まで呼べた。この抜け道は
  塞ぐ。どの Adapter を呼んだかは `provider` 列（診断用のラベル）に残す。
- 上限は `contracts.research.call_ceiling(limits, call)` だけで決める。`ResearchLimits` には
  `max_assessments` を新しく足した（旧実装には LLM 評価の枠が無かった）。
- `reserve` の手順: 同じ冪等キーの行があれば既存の行を返し、枠は消費しない。依頼が `running` で
  なければ拒否する。同じ入力に成否不明の行（dispatch 済みで決着していない）があれば
  `ResearchAmbiguousCallError` とする。金額と quota の見積りの合計が上限を超えるなら拒否する。
  `next = max(call_seq) + 1` が上限を超えるなら、INSERT せずに `ResearchBudgetExceededError` とする。
  INSERT は savepoint の中で行い、番号の衝突が起きたら読み直す。
- `reserved` / `spent` / `abandoned` のどれも枠を数える。`call_seq` は再利用しない。
  `abandoned` にできるのは dispatch 前の行だけで、これは DB の CHECK でも強制する。dispatch 後の失敗は
  `spent` として課金されたものとみなす（INV-15 / ADR-0036 と同じ保守的な扱い）。
- アプリの採番にバグがあっても、`UNIQUE(request_id, provider_call, call_seq)` と `call_seq >= 1` によって
  行数は上限を超えられない。DB 制約が最後の砦になる。

### 5. 成果物と再利用（鮮度キャッシュ）

- 本体は既存の ArtifactStore に置く。キーは research 専用の接頭辞
  `research/{request_id}/{artifact_type}/{sha256}.json`（`domain/research/keys.py`）で、Episode の
  `artifacts/` と混ざらない。リポジトリはこの形以外のキーを記録しない。
- 世代管理は本番と同じ考え方にする。同じ内容を再記録したら既存の行を返し、新しい内容は新しい
  `version` として現行を降ろす。A→B→A では過去の行を現行に戻す。現行は部分一意索引で
  `(request_id, artifact_type)` ごとに 1 本とする。
- 再利用の候補を返すのは `find_reusable(request_hash, not_older_than)` で、条件は `completed` であること
  （`partial` は返さない）、鮮度の窓の中にあること、現行の成果物を持つことの 3 つ。鮮度の窓（Trend 24 時間、
  Evidence 30 日）は呼び出し側が `not_older_than` に変換する。**再利用する前に、呼び出し側
  （B2 の Gateway）が本体を読み戻して sha256 を照合する**（base INV-31 と同じ考え方）。書き込みも、
  書く → 読み戻す → sha256 照合の後で記録する。

### 6. 予算の fail-closed（B2 で実装。INV にはしない）

実 Provider の依頼で `max_cost_usd` か `max_youtube_units` の**どちらか一方でも**未設定なら、
外部呼び出しの前に `blocked` にする。旧実装では文章が「両方」、コードが「どちらか」と食い違っていたので、
厳しい側に揃える。Provider の registry が知っているのは `fake` と `none` だけで、既定は `none` とする。
`none` は理由を記録して `blocked` にし、外部は呼ばない。

### 7. Episode 本番との関係（INV-37）

Research は補助機能として扱う。Research 以外の呼び出し側は、`completed` 以外の結果をすべて
「調査なし」として扱い、調査前の挙動で続ける。本番の工程（production / render / upload / storyboard /
pipeline / 課金）は Research を import しない（アーキテクチャテスト）。企画（Topic Planner）と台本
（Evidence ループ）への接続は B6 で opt-in（既定 OFF）として入れ、既定 OFF のときに出力が変わらないことを
その段で検査する。

### 8. Gateway・実行器・Worker（B2/B3 で追記）

- Gateway: 受け付け（冪等）、予算の fail-closed（§6）、鮮度キャッシュ（§5）、blocked の再開。
- 実行器: 外部呼び出しは必ず `reserve → commit → mark_dispatched → commit → 呼び出し → mark_spent` の
  順に進め、上限に達したら止めて `partial` にする。
- Worker: 専用の task queue に置き、Temporal の Activity retry で上限つきで再試行した後に
  `failed` / `partial` にする。compose / deploy / smoke / Makefile / docs は f67e95b の型に倣う。
- research の例外は `domain.errors` の型名の表（import 時に継承から導出される）に載らないので、
  Temporal の型名による分類では `needs_input` に落ちる（安全側）。Worker の段で扱いを決める。

### 9. 移植しないもの（決定）

- 本番の表での owner-XOR（Context のとおり）。
- Trend の定期更新 Schedule と `TREND_REFRESH_STALE` の異常種別。本番の Schedule と watchdog に触れるため。
  Trend は要求があったときに実行する。
- Storyboard の検証済み台本ゲート（旧 INV-31）と、Upload の出典つきメタデータ。storyboard と upload は
  本番側の所有のままにする。
- `POST /episodes/{id}/script`。base INV-30 の統一再開と衝突する。
- replay fixture `script_workflow_pre_0032_history.json`。visual style の型名を含むので、必要になったら
  撮り直す。
- 実 Provider の配線（§6）。

## Alternatives

- **(a) 旧 owner-XOR を移植する**: Context のとおり、本番のリポジトリと課金コードを書き換え、37 箇所の
  型を広げ、本番の CHECK 5 本と既存テストを変えることになる。Research を足すために 422 対策と
  課金安全性の中核に差分が入る。採らない。
- **(b) 呼び出し台帳を `provider_reservations` に同居させ、`episode_id` だけ NULL 可にする**: (a) の
  部分集合であり、`paid_job.py` の `provider_rejections` 記録（NOT NULL）の問題も同じく残る。採らない。
- **(c) 件数上限をアプリの採番だけで守る（DB 制約なし）**: 並行実行や採番のバグで上限を超えうる。
  お金に関わる上限は DB で止める（ADR-0021 の日次枠と同じ判断）。採らない。
- **(d) 枠を検索エンジンごとに持つ（旧実装）**: 検索エンジンを足すたびに実質の上限が増える。採らない。
- **(e) 本番の `ArtifactMetadataRepository` / `find_and_verify_current` を research でも使う**: Episode の
  キーと表を前提にしているので、使うには (a) の変更が要る。採らない（検証の考え方だけを共有する）。

## Consequences

**良い**
- `repositories.py`・`paid_job.py`・`contracts/states.py`・既存の migration と既存のテストは変わらない。
  本番の 422 対策と課金の経路に research の差分が入らない。
- 件数の上限が DB 制約になり、検索エンジンをまたいだ抜け道も塞がる。
- downgrade は research の行が残っていれば拒否し、支出の記録を黙って消さない。

**悪い / 引き受けた負債**
- 本番とほぼ同じ形のリポジトリ（世代管理、compare-and-set）を 2 つ持つ。直すときは両方を見る必要がある。
- `research_artifacts` は本番の `find_and_verify_current` を通らないので、再利用前の検証は research 側で
  別に実装する（B2）。判定の二重化を避けるという base INV-31 の文言は Episode の成果物に限った話として読む。
- 語彙を足すとき（例: 台本検証の成果物）は、新しい migration で research の CHECK を張り替える必要がある。
- research の例外は Temporal の型名の表に載らない（§8）。
- `max_assessments`（既定 5）は新しい値で、実データでの妥当性は未検証。
- 旧ブランチの Research 用 PostgreSQL 並行テストはまだ移植していない。SQLite では並行する 2 つの
  トランザクションを作れないので、単体テストは採番の競合を決定的に再現して代用している。
  本物の並行性は integration の段で見る。
