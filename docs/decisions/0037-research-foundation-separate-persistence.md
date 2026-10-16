# ADR-0037: Research の依頼・外部呼び出し台帳・成果物を本番の表から分離して永続化する

## Status

Accepted (2026-09-29)

範囲: この ADR は Research Tier B の土台を決める。B1（契約と永続化）は §1〜§5、B2（Gateway・実行器）は
§5〜§6 と §8.1〜§8.4、Worker・起動・API（B3）は §8.5 に書いた。
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

#### 8.5 Worker・起動・API（B3、`workers/research/`・`infrastructure/temporal/research_starter.py`・`apps/api/routers/research.py`）

**Workflow（`workers/research/workflows.py::ResearchWorkflow`）**

- `research_execute`（依頼全体を 1 回実行する。`ResearchExecutor.execute` の薄いラッパ）を 1 本だけ呼ぶ。
  失敗したら `research_record_failure`（`ResearchExecutor.record_failure(error_type=型名)`）を呼び、その結果を
  返す。**検索・取得ごとに Activity を分けない**（決定）。executor は 1 回の `execute` の中で順に呼び、
  再実行は成功済みの呼び出しを生データから読むので、分けても送り直しの防止は増えない。分けると
  Workflow が executor の分岐（§8.2 の表）を二重に持つことになる。
- I/O・壁時計・乱数・DB を使わない。依頼の期限は executor が DB の `started_at` から測るので、
  Workflow は `workflow.time()` も読まない。Activity は**名前**で呼ぶ（INV-3）。
- 履歴に載るのは `contracts/research.py` の dataclass（`ResearchWorkflowInput` / `ResearchExecuteRequest` /
  `ResearchRecordFailureRequest` / `ResearchWorkflowOutput` / `ResearchArtifactPointer`）だけで、依頼 ID・状態・
  理由コード・成果物の参照（id・sha256）・件数（検索・取得・評価・quota 単位・金額の文字列）に限る
  （ADR-0029 の「型注釈どおりの形」。成果物の本体・検索結果・本文は載せない）。
- `research_execute` の retry: `maximum_attempts = RESEARCH_EXECUTE_MAX_ATTEMPTS`（3。1 回目を含む）、
  指数バックオフ（5 秒から、最大 60 秒）。`non_retryable_error_types` は
  `domain/research/errors.py::RESEARCH_WORKER_NON_RETRYABLE_ERROR_TYPE_NAMES`（基底の
  `NON_RETRYABLE_ERROR_TYPE_NAMES` ∪ research の表。§8.4。Worker 側で 2 つの表を合わせ直さない）。
  retry は executor が新しい番号の予約を取るので、retry も合計で呼び出しの枠を数える（INV-36）。
- 時間: `start_to_close` は依頼の期限の天井（`LIMIT_CEILING_DEADLINE_SECONDS`）+ 30 分、heartbeat timeout は
  2 分。Activity は実行中に 20 秒ごとに heartbeat を送る。worker が落ちたら heartbeat timeout で retry され、
  executor が成否不明の呼び出しを送り直さずに `blocked`（`ambiguous_call`）にする。
- 失敗の記録: Activity の timeout（heartbeat を含む）は `TransientError` として記録する（retry を使い切った
  一時障害 = `failed`）。型名の分類は §8.4（`needs_input` は `blocked`、それ以外は `failed`、理由コードは
  `execution_failed`）。cancel は記録せずに伝える。

**Worker（`workers/research/run_worker.py`、compose service `research-worker`）**

- task queue は `contracts/research.py::RESEARCH_TASK_QUEUE`（`research`。唯一の定義）。登録するのは
  `ResearchWorkflow` と上の 2 つの Activity だけ。Provider・Handler・見積もりは registry（§8.2）が組む。
- 環境は DB・MinIO・Temporal（`x-app-env`）と `RESEARCH_PROVIDER` だけ。`YOUTUBE_*` / `CODEX_*` / `FAL_KEY` を
  渡さない。Codex の sandbox 例外（cap_add / seccomp）も持たない。Workflow を持つ queue なので、Activity 専用
  queue の長い max-age は使わない（healthcheck は既定の max-age で `research` を見る）。
- `RESEARCH_PROVIDER` の既定は `none`（依頼は外部を呼ばずに `blocked`）。受け付けの門（api）と実行の門（worker）
  が**同じ変数**を読む（compose の api にも同じ既定で渡す）。片方だけ `fake` にしても、もう片方の門で
  `blocked` になる（fail-closed）。
- 未知・誤記の `RESEARCH_PROVIDER` は設定の読み込みで `none` へ落とし、警告を出すだけで起動は止めない
  （2026-09-30 追記。B6 で script-worker も同じ変数を受け取るため、検証エラーにすると Research を使っていない
  日次の企画・台本の worker まで起動できなくなる。独立レビューの指摘による。検査:
  `tests/unit/test_research_registry.py::test_only_fake_and_none_are_modes_and_an_unknown_value_falls_back_to_none`）。
- compose / deploy / smoke / Makefile / docs は f67e95b（scene-alternative-worker）の型に倣う:
  `compose.yaml`、`scripts/deploy-workers.sh` の `APP_SERVICES`、`scripts/smoke-workers.sh` の `QUEUES` と
  `ALL_WORKERS`、`Makefile` の `workers-logs`、`docs/operations/workers.md`。旧ブランチは smoke と Makefile を
  漏らしていた。
- **既存テストの変更（この ADR が承認する）**: 新しい worker を列挙した集合に足すだけで、検査は緩めない。
  `tests/contract/test_compose_workers.py` の `WORKERS` に `research-worker` を足す（Activity 専用ではないので
  `ACTIVITY_ONLY_MIN_MAX_AGE` には足さない。`YOUTUBE_` / `CODEX_` / `FAL_KEY` の持ち主と `CODEX_WORKERS` は
  変えない）。`tests/unit/test_deploy_scripts.py` の `APP` に足す。`tests/architecture/test_research_isolation.py`
  の検査対象（`RESEARCH_PATHS` / `RESEARCH_MODULE_PREFIXES`）に起動と API のモジュールを足す（検査が広がる側）。

**起動（`infrastructure/temporal/research_starter.py`）**

- workflow id は依頼 1 件につき 1 つ（`research_workflow_id` = `research-{request_id}`）。実行中の同じ id は
  Temporal が構造的に拒否する（`WorkflowAlreadyStartedError`）ので、同じ依頼を同時に 2 つ走らせない。拒否は
  「もう走っている」として成功扱いにし、同じ id を返す。
- id の再利用は `ALLOW_DUPLICATE`。`blocked` で止めた workflow は Temporal の上では成功終了しているので、
  `ALLOW_DUPLICATE_FAILED_ONLY` だと再開（`blocked → queued`）した依頼を二度と走らせられない。終了済みの id で
  再び走らせても executor は終わった依頼を書き換えず、成功済みの呼び出しを送り直さない。**完了は待たない**
  （INV-16）。

**API（`apps/api/routers/research.py`。追加だけ）**

- `POST /research/requests`: `ResearchGateway.submit`（§8.1）の後、依頼が `queued`（新規、または保存と起動の
  間で落ちて `queued` のまま）なら起動して 202。`blocked`（Provider 未設定・予算未設定）・再利用・`running` 以降の
  依頼は起動しない。同じ冪等キーの再 POST が「保存と起動の間で落ちた依頼」の回収経路になる。
- `POST /research/requests/{id}/resume`: `ResearchGateway.resume`（同じ門）が `blocked → queued` にしたときだけ
  起動する。門を通らない・`blocked` でない依頼は 409、未知の依頼は 404。
- `GET /research/requests/{id}`: DB だけを読む（Temporal に問い合わせない。INV-8）。
- Episode のエンドポイントは変えない。`POST /episodes/{id}/script` は作らない（§9）。

**Episode 本番との関係（INV-37）**: 日次・Episode pipeline・企画・台本の workflow とその worker は、Research の
workflow・queue・起動・Gateway・実行器を名指さない
（`tests/architecture/test_daily_does_not_wait_for_research.py`）。Research の workflow を登録する worker は
research-worker だけ。

**読み手・書き手（AGENTS.md §8 の grep 記録、B3）**: `git grep -n "<key>" 74a8bfa -- apps workers domain
infrastructure contracts docs compose.yaml scripts Makefile .env.example` を変更の前に実行した結果:

| キー | 件数 | 分類 |
|---|---|---|
| `RESEARCH_TASK_QUEUE` / `RESEARCH_WORKFLOW` / `research_workflow_id` | 0 / 0 / 0 | 新規（`contracts/research.py` が唯一の定義。読み手は worker・起動・compose の検査） |
| `research_execute` / `research_record_failure` | 0 / 0 | 新規（Activity 名。`contracts/research.py`） |
| `RESEARCH_WORKER_NON_RETRYABLE_ERROR_TYPE_NAMES` | 0 | 新規（`domain/research/errors.py`。読み手は Workflow の RetryPolicy だけ） |
| `research-worker` | 1 | 無関係（`infrastructure/youtube/search.py` の docstring が「YOUTUBE_* を持たない」と言及するだけ） |
| `RESEARCH_PROVIDER` | 12 | 読み手 1（`infrastructure/config.py` の `research_provider`）、書き手 0（B3 で compose の api / research-worker と `.env.example` が書き手になる）、残り 11 は定義（`DEFAULT_RESEARCH_PROVIDER` / `RESEARCH_PROVIDER_MODES`）・docstring・設計書の言及 |

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
- **(j) 検索・取得ごとに Activity を分ける（旧実装: plan / search / select / fetch / synthesize）**: Workflow が
  executor の再実行の分岐（§8.2）と期限・停止の判断を二重に持つことになり、Activity の結果（検索結果の
  要約など）が履歴に増える。送り直しの防止は台帳と生データが担うので、分けても安全性は増えない。
  採らない（§8.5。1 本の `research_execute` と失敗の記録だけ）。
- **(k) 起動の id 再利用を `ALLOW_DUPLICATE_FAILED_ONLY` にする（旧実装の通常の起動）**: `blocked` の依頼は
  Workflow としては成功終了するので、再開した依頼が二度と走らない（旧実装は再開だけ別の方針にしていた）。
  採らない（常に `ALLOW_DUPLICATE`。実行中の二重起動は Temporal が拒否し、終了済みの再実行は executor が
  冪等に扱う）。
- **(l) Research の API を既存の `WorkflowStarter` / `apps/api/dependencies.py` に足す**: 既存の Protocol に
  メソッドを足すと、Episode の API のテスト用 fake まで直すことになる。採らない（Research 専用の
  `ResearchWorkflowStarter` と依存をルータに置く）。

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
  成功済みの呼び出しは生データから読むので送り直さない。Activity は分けない（§8.5 で決定）。
- B3 で既存テスト 2 本（`tests/contract/test_compose_workers.py` の `WORKERS`、`tests/unit/test_deploy_scripts.py`
  の `APP`）の列挙に `research-worker` を足した。新しい worker を足すたびに同じ列挙を更新する前例
  （f67e95b）どおりで、検査は緩めていない（§8.5）。
- 受け付けの門（api）と実行の門（worker）が同じ `RESEARCH_PROVIDER` を別々のプロセスで読む。compose は同じ
  変数を渡すが、片方だけ値を変えると依頼は `blocked` になる（安全側だが、気づきにくい。運用手順に書いた）。
- `research_execute` の最長時間は依頼の期限の天井（24 時間）+ 30 分で、依頼ごとの期限より長い。固まった
  worker は heartbeat timeout（2 分）で検出する。
- 旧ブランチの Research 用 PostgreSQL 並行テストはまだ移植していない。SQLite では並行する 2 つの
  トランザクションを作れないので、単体テストは採番の競合を決定的に再現して代用している。
  本物の並行性は integration の段で見る。
