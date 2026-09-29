# ADR-0037: Research の依頼・外部呼び出し台帳・成果物を本番の表から分離して永続化する

## Status

Accepted (2026-09-29)

範囲: この ADR は Research Tier B の土台を決める。B1（契約と永続化）は §1〜§5、B2（Gateway・実行器）は
§5〜§6 と §8.1〜§8.4 に書いた。Worker（B3）は §8.5 に方針だけを書き、その段で同じ ADR に追記する。
Evidence（B4）は ADR-0038、Trend（B5）は ADR-0039 に分ける。

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
- **再利用前の検証（B2、`infrastructure/research/gateway.py::ResearchGateway._verified`）**: 候補の
  `result_summary.artifact_refs` の各成果物について、(1) その `(id, 型, sha256)` がこの依頼の**現行の**
  `research_artifacts` 行であること、(2) 行の `object_key` が `research_artifact_object_key(依頼, 型, sha256)`
  そのものであること、(3) ArtifactStore から流して読んだ sha256（`readback_sha256`）が記録と一致すること、
  の 3 つを確かめる。どれかが外れたら（本体が無い `KeyError` も含む）**再利用せず**、新しい依頼として
  保存・実行する（候補の行は変えない。壊れた本体の扱いは人が見る）。最新の候補 1 件だけを見て、
  それが外れても古い候補へは遡らない（古い方が新しい方より信頼できる根拠は無い）。
- 鮮度の窓は設定値 `TREND_FRESH_HOURS` / `EVIDENCE_REVERIFY_DAYS`（既定は `contracts/research.py`）で、
  壁時計（注入できる時計）から測る。`finished_at` もリポジトリが壁時計で書くので同じ時計で比べる。
- 冪等キーの一致は再利用より先に判定する（同じキーの再送は、鮮度に関係なく同じ依頼を返す）。
  再利用したときは新しい冪等キーを記録しない（新しい行を作らない）。

### 6. 予算の fail-closed（B2 で実装。INV にはしない）

実 Provider の依頼で `max_cost_usd` か `max_youtube_units` の**どちらか一方でも**未設定なら、
外部呼び出しの前に `blocked` にする。旧実装では文章が「両方」、コードが「どちらか」と食い違っていたので、
厳しい側に揃える。Provider の registry が知っているのは `fake` と `none` だけで、既定は `none` とする。
`none` は理由を記録して `blocked` にし、外部は呼ばない。

B2 の実装:

- 判定は `domain/research/admission.py` の `lacks_budget` / `admission_block` の**1 か所**。Gateway の受け付け
  と再開、実行器の開始が同じ関数を呼ぶ（Gateway を経由しない依頼作成の経路が後で増えても、実行器の開始で
  止まる）。順序は「Provider 未設定（`provider_not_configured`）→ 予算未設定（`budget_not_set`）」。
- `blocked` にするときも `queued → running → blocked` と進め、`started_at` を残す（§3 の表のとおり）。
  `blocked_reason` は `{"code", "detail"}`、`result_summary.stop_code` にも同じコードを入れる。
- 理由コードの語彙は `contracts/research.py::ResearchStopCode` が唯一の定義（DB の CHECK には載せない）。
- 依頼が上限を指定しなかった場合、Gateway は設定値 `RESEARCH_MAX_COST_USD` / `RESEARCH_MAX_YOUTUBE_UNITS`
  （既定は未設定）を依頼に凍結する。明示された上限は上書きしない。
- `resume` は同じ門を通す。凍結した上限が足りない依頼（`budget_not_set`）は再開しない（resume で上限は
  増えない。新しい冪等キーで出し直す）。Provider が未設定のままの依頼も再開しない。
- registry は未知の設定値を「実物・未設定」として扱う（どちらの判定でも止める側に倒れる）。
  `provider_config_version` に設定値を足す（`provider-config-1+fake`）ので、Fake の結果は別の設定の
  依頼に再利用されない。

### 7. Episode 本番との関係（INV-37）

Research は補助機能として扱う。Research 以外の呼び出し側は、`completed` 以外の結果をすべて
「調査なし」として扱い、調査前の挙動で続ける。本番の工程（production / render / upload / storyboard /
pipeline / 課金）は Research を import しない（アーキテクチャテスト）。企画（Topic Planner）と台本
（Evidence ループ）への接続は B6 で opt-in（既定 OFF）として入れ、既定 OFF のときに出力が変わらないことを
その段で検査する。

### 8. Gateway・実行器・Worker

#### 8.1 Gateway（B2、`infrastructure/research/gateway.py`）

独立したプロセスではなく、API と Worker が呼ぶ関数の集まり。`submit` の順序は、契約から依頼を作る
（未指定の金額・quota の上限を設定値から凍結）→ `request_hash` → 同じ冪等キーの依頼があれば返す（意味が
違えば `ResearchIdempotencyConflictError`）→ 鮮度キャッシュ（§5。検証つき）→ `create_or_get` → 予算の門
（§6）。Workflow の開始はしない（B3 で足す）。`resume` は `blocked → queued` を compare-and-set で行い、
同時に 2 回呼んでも 1 つだけが再開する。

#### 8.2 実行器（B2、`infrastructure/research/executor.py`）

Temporal を知らない。Worker（B3）の Activity は `execute(request_id)` と `record_failure(...)` の薄い
ラッパになる。

- **外部呼び出し 1 件の順序**: `reserve → commit → mark_dispatched → commit → 呼び出し → 生データの保存
  → mark_spent → commit`（ADR-0013 / INV-15 と同じ規律）。呼び出しキーは
  `research:{request_id}:{call}:{input_hash[:16]}`（2 回目以降は `:r{n}`）で、Temporal の attempt を
  含めない。検索の入力 hash は query の正準 JSON、取得の入力 hash は正規化 URL（`domain/research/urls.py`）。
- **生データ**: 検索結果と本文は `research/{request_id}/raw/{call}/{call_id}.json`（`domain/research/keys.py`）
  に置く。成果物ではない（`research_artifacts` に記録しない）。「呼んで結果を受け取った」証拠で、
  再実行はこれを読み、同じ呼び出しを送り直さない。
- **再実行の分岐**（同じ入力の最新の行）: 生データのある `spent` は読むだけ。生データのある `reserved`
  （呼んだ後・spent の前に落ちた）は `spent` にして読む。生データの無い dispatch 済み `reserved` は成否不明
  なので**送り直さず解放もせず**、依頼を `blocked`（`ambiguous_call`）にする。dispatch 前の `reserved` は
  その行で続ける。生データの無い `spent` は、恒久的な失敗なら送り直さず、一時障害・停止なら新しい番号で
  予約する（retry も合計で枠を数える。INV-36）。どの分岐でも B1 のリポジトリの規則（dispatch 済みの行は
  `abandoned` にできない、同じ入力に成否不明の行があれば予約しない）を迂回しない。
- **失敗の扱い**: 一時障害（5xx・通信断・timeout）は `spent` に確定してから
  `ResearchSourceUnavailableError`（retryable）を送出する。予約を `reserved` のまま残さない。rate limit・
  quota 枯渇・認証拒否・Provider 未設定は `spent` に確定し、その種別の以後の呼び出しを止める。上限（件数・
  金額・quota）に達したときは予約の前に止まる（行を作らない）。恒久的な失敗（入力の拒否、取得できない URL）は
  その 1 件の穴として記録して続ける。分類できない例外は予約を dispatch 済みのまま（成否不明）にして伝える。
- **終わり方**: 成否不明の呼び出しがあれば `blocked`。使える検索結果が 1 つも無ければ、止めた理由があれば
  `blocked`（期限切れだけは `failed`）、無ければ `failed`（`no_usable_results`）。使える結果があれば
  Handler が成果物を組み、実行しなかった検索・取得、穴、止めた理由、Handler の申告のどれかがあれば
  `partial`、無ければ `completed`。`partial` は合格ではない（§3）。
- **期限**: 依頼に凍結した `deadline_seconds` を `started_at` から測り、過ぎたら以後の呼び出しをしない。
- **成果物**: 正準 JSON を `research/{request_id}/{type}/{sha256}.json` に `put_json` し、`readback_sha256`
  で読み戻して照合してから `research_artifacts` に記録し、同じ commit で依頼を `completed` / `partial` に
  進める。照合が合わなければ記録しない（`ResearchArtifactReadbackError`、retryable）。
- **URL の取得**は注入された `ContentFetcher`（Tier A の `HttpContentFetcher` は `UrlGuard` を必ず通る。
  または `FakeContentFetcher`）だけを通る。registry・Gateway・実行器は実 Provider を import しない
  （`tests/architecture/test_research_isolation.py::test_research_execution_does_not_wire_a_real_provider`）。
- quota の見積もりは YouTube 検索 1 回につき `quota_costs.YOUTUBE_FULL_SEARCH_UNITS`。金額の単価は、実
  Provider が無いのでまだ持たない（見積もり無し）。

#### 8.3 Handler の境界（B2、`domain/research/handlers.py`）

種別ごとの判断（何を検索し、どれを取得し、結果から成果物をどう組むか）だけを `ResearchHandler` の背後に
置く。純粋で決定的（再実行は同じ計画から同じキーを作る）。上限は Handler を信用せず、実行器が
`plan_within_ceiling` / `dedupe_fetch_targets` で切る（計画が上限を超えた分は `partial` の理由になる）。
Handler の出力は、成果物の型が Handler の型と一致し、`request_id` がこの依頼であることを実行器が検査する
（外れたら `ResearchOutputInvalidError`、permanent）。B2 では種別ごとの Handler を登録しない
（`registry.build_handlers` は空。Handler の無い種別の依頼は `blocked`、`handler_not_available`）。
Evidence（ADR-0038）と Trend（ADR-0039）の Handler はここに登録する。LLM による評価
（`ResearchCall.ASSESS`）は `synthesize` の中で外部を呼ばず、実行器に評価の段を足して同じ台帳を通す。

#### 8.4 research の例外と Temporal の型名による分類（B2 で決定）

`domain.errors.FAILURE_CLASS_BY_TYPE_NAME` は `domain/errors.py` の import 時に継承から作られるので、
`domain/research/errors.py` の型は載らない。その表だけで引くと、research の型は安全側の `needs_input` に
落ち、`NON_RETRYABLE_ERROR_TYPE_NAMES` にも入らない（permanent な research の例外を Temporal が retry する）。

**決定**: `domain/errors.py` は変えず、`domain/research/errors.py` の import 時に research 側の表
`RESEARCH_FAILURE_CLASS_BY_TYPE_NAME` と `RESEARCH_NON_RETRYABLE_ERROR_TYPE_NAMES` を**同じ継承の規則から**
作る。各型の失敗クラスは、MRO の中で最初に現れる `domain.errors` の型を基底の表で引いた値で、失敗クラスを
ここに書き直さない。`research_failure_class_from_type_name` は research の表を引き、無ければ基底の表へ
落ちる（未知の名前は `needs_input`）。research の例外はすべて `domain/research/errors.py` に置く
（別のモジュールに置くと表に載らない。テストで固定する）。基底の型名と同じ名前は import 時に拒否する。

B3 の Worker は、research の Activity の `RetryPolicy.non_retryable_error_types` に
`NON_RETRYABLE_ERROR_TYPE_NAMES + RESEARCH_NON_RETRYABLE_ERROR_TYPE_NAMES` を渡し、retry を使い切った失敗は
`ResearchExecutor.record_failure(error_type=型名)` で記録する（`needs_input` なら `blocked`、それ以外は
`failed`、理由コードは `execution_failed`）。

#### 8.5 Worker（B3 で追記）

- 専用の task queue に置き、Temporal の Activity retry で上限つきで再試行した後に
  `failed` / `partial` にする。compose / deploy / smoke / Makefile / docs は f67e95b の型に倣う。

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
- **(f) research の例外を `domain/errors.py` に足す（旧実装）**: 型名の表にそのまま載るが、本番の失敗の表に
  research の型が 10 以上混ざり、INV-37 の分離（本番のコードが research を知らない）を崩す。採らない。
- **(g) Activity の境界で research の例外を基底の型（`NeedsInputError` など）に変換してから送出する**:
  表は変えずに済むが、履歴と診断から `ResearchAmbiguousCallError` のような具体的な型名が消え、
  `record_failure` での区別もできなくなる。採らない（§8.4 の research 側の表を採る）。
- **(h) 検証に失敗した再利用候補の代わりに、より古い候補を探す**: 古い方が信頼できる根拠が無く、
  壊れた本体を持つ依頼を黙って飛ばすことになる。採らない（新しい依頼として実行する）。
- **(i) 一時障害の予約を `abandoned` に戻して同じ番号で再送する**: 送った呼び出しは課金されうるので
  `abandoned` にできない（§4）。採らない（`spent` にして新しい番号を取る）。

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
- 一時障害の retry は新しい番号を使うので、一時障害が続くと上限に早く達し、結果は `partial` になる
  （お金を守る側に倒した）。
- research の型名による分類は表が 2 つになる（基底の表と research の表）。Worker は両方を合わせて渡す
  必要がある（§8.4。B3 のテストで固定する）。
- 再利用の検証は候補ごとに成果物の本体を全部読む。成果物が大きくなると受け付けが遅くなる。
- 実行器は検索・取得を 1 つの `execute` の中で順に行う。Temporal の retry は依頼全体をやり直すが、
  成功済みの呼び出しは生データから読むので送り直さない。検索・取得ごとに Activity を分けるかは B3 で決める。
- 旧ブランチの Research 用 PostgreSQL 並行テストはまだ移植していない。SQLite では並行する 2 つの
  トランザクションを作れないので、単体テストは採番の競合を決定的に再現して代用している。
  本物の並行性は integration の段で見る。
