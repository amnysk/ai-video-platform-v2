# ADR-0038: Evidence Research と台本の照合を research の成果物として実装する

## Status

Accepted (2026-09-29)

範囲: Research Tier B の B4（Evidence Research と台本の照合）。土台（依頼・台帳・Gateway・実行器・
Worker）は ADR-0037。台本工程（ScriptWorkflow）への接続は B6 で、既定 OFF の opt-in として別に決める。

## Context

旧ブランチ `claude/research`（tip `8cede47`）の旧 ADR-0032「Evidence Research・最終台本の検証・概要欄の
出典」は、次の 4 つを 1 つの ADR で決めていた。

1. 主張（claim）単位の Evidence 成果物と、評価器（LLM）の提案をコードで検査して確定する規則（§1〜§3）
2. 最終台本を Evidence と照合する `script_verification`（Episode 所有の Artifact。§4）
3. 照合と書き直しのループを `ScriptWorkflow` に置き、Storyboard の入場を検証済み台本だけに絞る（§5、旧 INV-31）
4. YouTube 概要欄の出典ブロック（`upload_metadata` Artifact）と Upload の照合（§6）

本番系統（f209e7c 以降）は ADR-0037 で research の永続化を本番の表から切り離した。旧実装の 2〜4 は本番の
`ArtifactType`・`artifact_metadata`・Storyboard / Upload の Activity・Episode API に手を入れる前提で
書かれており、そのままでは移植できない。また、旧実装の評価器の呼び出し（`EvidenceHandler.synthesize` の
中の `await assessor.assess`）は、予約台帳を通らない**予算外の呼び出し**だった（旧 inventory §0.6:
`RESEARCH_ASSESS` は定義だけで一度も予約されない）。

**移植しないもの（決定）**:

- **Storyboard の検証済み台本ゲート（旧 INV-31）**: storyboard は本番側の所有のまま（ADR-0037 §9、INV-37）。
  Research の結果で本番の工程を止める経路を作らない。
- **Upload の出典つきメタデータ（`upload_metadata` Artifact、`sources_block`、`upload_metadata_builder`）**:
  upload は本番側の所有のまま。INV-14 の upload key と投稿済み動画の不変性に触れない。
- **`POST /episodes/{id}/script`**: base INV-30（統一再開エントリポイント、ADR-0032）と衝突する。
- **`ScriptWorkflow` の Evidence ループ**: B6 で opt-in（既定 OFF）として別に扱う。B4 は照合の**操作**だけを持つ。
- 旧 `claim_language.py`（約 1000 行の語彙）の全量。照合に要る信号（量化子・因果・最上級・数値・確度・
  引用・異説）だけを `evidence_text.py` に置く。

## Decision

### 1. 成果物の契約（`contracts/research_evidence.py`）

`research_evidence` と `research_script_verification` は ADR-0037 の語彙（migration 0015 が凍結済み）の
research 成果物で、新しい migration は要らない。契約の validator が参照整合性の最終防衛線:

- link の claim / source は実在する。**本文を完全に確認した資料（`fetch_status="fetched"`）だけ**が
  `supports` / `qualifies` の根拠になる（旧実装は `truncated` も許していた。取得の切り詰めを確認済みにしない）。
- `supported` は `supports` を要し、`refutes` があれば作れない。強い主張（`causal` / `quantity` /
  `superlative`、または central で異説あり）の `supported` は origin の異なる `supports` 2 件以上、うち 1 件は
  一次・学術・機関の資料。
- 各 claim に `assessed` を持つ。**評価していない claim は `insufficient` だけ**で、link を持たない。
- 資料の URL は `FetchedContent.final_url` だけ。検索結果の snippet だけの資料は載せない（旧実装の
  `snippet_only` は廃止）。URL は http(s)・host あり・認証情報なし。
- 評価器の出力 schema `AssessmentProposal` は strict（全 property required・`extra=forbid`）。

### 2. 評価の規則（`domain/research/evidence_rules.py`。純粋）

評価器の出力は**提案**。コードが検査して `min(提案, コード評価)` で確定する（順序 supported > qualified >
disputed > insufficient）。規則は旧 ADR-0032 §2 と同じ意味: 抜粋の実在（本文の部分文字列に置き換えて
保存）、年代の不一致の除外、範囲・因果・最上級の降格、独立性、異説。変更点: 提案が取得していない URL・
存在しない資料を指したら、旧実装のように依頼全体を失敗させず、**その claim を「評価していない」
（`insufficient`、`partial` の理由）にする**（fail-closed だが他の claim の結果は残す）。

### 3. 実行（`EvidenceHandler` と実行器の評価の段）

- `EvidenceHandler`（`domain/research/evidence_handler.py`）は ADR-0037 §8.3 の `ResearchHandler` と、
  新しい `AssessingHandler.plan_assessments` を実装する。検索語は claim 本文から機械的に抜いた語だけ
  （URL・書誌を作らない）。検索の段 ID は `c001-primary` の形で claim に戻せる。
- **評価器の呼び出しは実行器が台帳を通して行う**（ADR-0037 §8.3 の予告どおり）。`ResearchCall.ASSESS`、
  枠は `max_assessments`（INV-36）、書き込み順序・再実行の分岐・生データ（`raw/assess/`）は検索・取得と
  同じ。入力 hash は claim と passage の正準 JSON。`synthesize` は外部を呼ばない。
- 評価器の出力が schema に合わない: `ResearchOutputInvalidError`（permanent）としてその 1 件の穴にする。
- 評価器が組まれていない（`ResearchProviders.assessor=None`）: 呼ばずに `assessor_not_available`
  （`ResearchStopCode` に追加）で `partial`。枠を越える評価は送らずに `partial`（`call_budget_exhausted`）。
- 実行器は成果物の `sources[].url` がこの実行で取得した `final_url` に属することを確かめてから保存する
  （Handler を信用しない）。
- 完了: 全 claim が評価済み、または検索・取得したが関連する記述が無かった場合だけ `completed`。
- 鮮度: 同じ意味の完了済み Evidence は Gateway の検証つき再利用（ADR-0037 §5、`evidence_reverify_days`）で
  返す。新しい仕組みは足さない。
- registry: `EvidenceHandler` は設定に依らず登録する（`none` の依頼は実行器の門で先に `blocked`）。
  `fake` は `FakeEvidenceAssessor` / `FakeClaimExtractor` を組み、`none` は組まない。実 LLM は配線しない。

### 4. 台本の照合（`research_script_verification`）

- 入力 `ScriptVerificationRequest`: Evidence の依頼 ID、その依頼の現行の `research_evidence` の参照
  （id・sha256）、台本の文面（`unit_id` = `title` / `hook` / `scene:sN:narration|visual`）。書き手の claim id は
  受け取らない。`script_ref` と `episode_id` は**参照だけ**。
- 操作 `ScriptVerifier.verify`（`infrastructure/research/verification.py`）: Evidence の成果物が現行・research
  のキー・読み戻しの sha256 一致であることを確かめ（外れたら照合しない）、純粋な規則
  （`domain/research/script_verification.py`）で照合し、結果を research のキーに書き・読み戻して照合して
  から **Evidence の依頼の成果物として記録**する。依頼の状態は変えない。外部を呼ばない（台帳に行を作らない）。
- 結論は 3 値: `passed`（Evidence が `completed`、全文面が根拠の範囲内、未登録の主張なし）/ `failed`
  （強すぎる表現・異説を事実として述べる・未登録の主張）/ `insufficient`（Evidence が `completed` でない、
  `insufficient` / 評価していない claim に依拠）。契約が「`passed` は `completed` の Evidence だけ」を強制する。
- `ClaimExtractor` は決定的な網への上乗せ。実 LLM の抽出器は配線しない（配線するなら、その呼び出しを
  台帳の枠と金額の上限に載せる ADR を先に置く）。

### 5. 不変条件

新しい INV は足さない。「予算外の LLM 呼び出しをしない」は INV-36（全外部呼び出しが台帳の枠を通る）の
適用で、`tests/unit/test_research_evidence_executor.py` が評価の行数を検査する。「評価していない根拠で合格に
しない」は契約の validator と ADR のこの節で足りる。

### 6. 既存テストの変更（この ADR が承認する）

- `tests/unit/test_research_registry.py` の `test_no_kind_specific_handler_is_registered_yet`（B2）を
  `test_only_the_evidence_handler_is_registered` に置き換えた。旧テストの docstring 自身が「後続の段で登録
  する」と予告していた仕様の変更で、Trend が未登録であることは引き続き検査する（緩めていない）。
  （追記 2026-09-30: B5 で Trend を登録したので、ADR-0039 §6 がこのテストを
  `tests/unit/test_research_registry.py::test_exactly_the_evidence_and_trend_handlers_are_registered`
  に置き換えた。）
- `tests/architecture/test_research_isolation.py` の検査対象に新しいモジュールを**足した**（検査が広がる側）。

**読み手・書き手（AGENTS.md §8 の grep 記録、B4）**: `git grep -n "<key>" 8e0f8cb -- apps workers domain
infrastructure contracts docs`:

| キー | 件数 | 分類 |
|---|---|---|
| `assessor_not_available` | 0 | 新規（`contracts/research.py::ResearchStopCode` が唯一の定義。書き手は実行器） |
| `research_script_verification` | 2 | 定義 1（`contracts/research.py`）・migration 0015 の凍結 1。書き手は B4 の `ScriptVerifier` だけ |
| `build_claim_extractor` / `assessor:` | 0 / 0 | 新規（registry と `ResearchProviders` の欄） |

## Alternatives

- **(a) 旧実装どおり `synthesize` の中で評価器を呼ぶ**: 台帳を通らない予算外の LLM 呼び出しになる（INV-36
  の穴）。再実行で同じ評価を送り直す。採らない。
- **(b) 評価を claim ごとの Activity に分ける**: ADR-0037 §8.5 (j) と同じ理由（Workflow が実行器の分岐を二重に
  持つ）。採らない。
- **(c) 照合結果を Episode の Artifact（`script_verification`、本番の `ArtifactType`）にする（旧実装）**:
  本番の語彙と表を変える（ADR-0037 の分離を崩す）。採らない。research の成果物として Evidence の依頼が所有する。
- **(d) 照合専用の依頼種別（`kind=verification`）を足す**: migration で research の CHECK を張り替える必要が
  あり、照合は外部を呼ばないので台帳も要らない。採らない。
- **(e) 結論を旧実装の `passed / needs_followup / blocked` にする**: follow-up の回数は台本ループ（B6）の
  関心で、照合そのものは「台本の問題」と「根拠の不足」を区別すれば足りる。`passed / failed / insufficient` にする。
- **(f) 取得の切り詰め（`truncated`）も根拠に数える（旧実装）**: 本文を最後まで確認していない。採らない。
- **(g) 評価器の参照違反で依頼全体を permanent に失敗させる（旧実装）**: 1 件の悪い提案で他の claim の結果を
  失う。採らない（その claim だけ未評価にする。合格にはしない）。

## Consequences

**良い**
- 評価器の呼び出しも台帳の枠・生データ・再実行の分岐を通る。再実行は保存済みの提案を読み、送り直さない。
- 未取得・切り詰めの資料、作られた URL・抜粋、評価していない claim から合格が作れない（契約と規則の二重）。
- 本番の表・語彙・Storyboard / Upload・Episode API は変わらない。migration も増えない。

**悪い / 引き受けた負債**
- Fake の評価器は意味を理解しない。Fake で検査しているのは契約・規則・配線で、評価の妥当性ではない。
- `origin_key`・`source_kind`・照合の語彙は近似（ドメイン・語の重なり）。過検出は `failed` / `insufficient`
  に倒れる（安全側）が、見逃しもある。
- 照合結果は Evidence の依頼の「現行」を世代で持つ。同じ Evidence で複数の台本を照合すると現行は最新の
  1 件で、古い結果は `superseded` の行として id で辿る（呼び出し側は返った参照を保持する）。
- 既存の実行器の規則どおり、取得 1 件の一時障害（timeout）は依頼全体の retry になり、続けば `failed` になる
  （Fake コーパスの `TIMEOUT_URL` に当たる 関ヶ原 の claim が該当）。取得の一時障害を穴として扱うかは
  ADR-0037 §8.2 の変更になるので、ここでは変えずに記録する。
- 台本工程への接続（B6）が入るまで、照合は API や Workflow からは呼ばれない（操作とテストだけ）。
