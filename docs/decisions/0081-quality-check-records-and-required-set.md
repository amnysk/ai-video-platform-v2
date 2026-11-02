# ADR-0081: 品質検査の記録（DB）・必須検査の集合・手動承認の記録（既定 OFF・fail-closed）

## Status

Accepted（v3 Phase 3 の親エージェント割当。2026-10-30）。`AVP_QUALITY_GATE_ENFORCE`（既定 OFF）の背後にだけ効く。
本番へ migration 0019 を適用するか・flag を ON にするかは所有者の判断で、本 ADR は決めない。
ADR-0076 §3/§4 が「後続」とした 3 点（レポートの永続保存・DB 上の承認記録・gate 時点の検査入力）を扱う。

## Context

ADR-0076 の gate は final_video の `sha256` / `input_hash` しか読めず、技術 QA・説明欄の検査に入力が渡らない。
決定論の検査が 0 件になり、ON にすると全件 REVIEW = 全件拒否になる（Phase 2 報告 §3.1/§4）。足りないものは 4 つ。

1. 検査結果の保存先（どの動画・どの規則版・いつの結果かを構造で結ぶ）。
2. 技術 QA・説明欄の入力の供給（説明欄の入力生成器そのものが無い。upload 工程は `composition` を渡していない）。
3. BGM・地図の検査が「使われたとき必須、使われないとき明示の記録」になる仕組み（他の枝が純関数を作る）。
4. 手動投稿で REVIEW を承認した人・時刻・対象 hash の DB 記録。

実測した事実（実装時点）:

- `final_video` の `artifact_metadata.sha256` は、完成動画 JSON（正準形）の hash。JSON は `media.sha256`・`measured`・
  **`technical_qa`（合格時のみ保存）** を含む。つまり gate は、JSON を読んで正準形の hash が `sha256` と一致することを確かめれば、
  技術 QA を**その hash に結び付いた形で**導出できる（新しい保存先なしで）。
- BGM の使用証跡は object storage に key 付きで置かれ（`bgm_render_evidence`、行なし）、prefix 列挙の Port が無い。
  render 入力 hash から「BGM 無し」を逆算するには font hash・engine 同一性・スレッド数が要り、gate では再現できない。
  よって「BGM / 地図が使われたか」は gate が推定せず、**render が宣言した事実**を読む（§4）。
- 手動 `POST /episodes/{id}/upload` が起動する upload workflow は `upload_gate` を通らない（`PIPELINE_UPLOAD_GATE` は日次の
  `EpisodePipelineWorkflow` だけ）。手動経路の判定は API 内で完結する。

## Decision

### 1. 「対象」の定義（主語を 1 つに固定する）

- **対象 hash（`subject_sha256`）** = 現行 `final_video` の `artifact_metadata.sha256`（完成動画 JSON の正準 hash。
  `QualityReport.final_video_sha256` と同じ値）。**入力 hash（`input_hash`）** = 同じ行の `input_hash`。
- 検査記録は「この対象 hash の完成動画について、この規則版で、この時刻に、この producer が出した」という主張。
  対象 hash と `input_hash` の両方が今の現行行と一致しなければ数えない。再 render（新しい sha）、規則版の更新、
  期限切れ、世代交代（superseded）のどれでも古い記録は数えない。

### 2. 保存先（migration 0019。新しい表 4 つだけ・expand-only）

| 表 | 役割 | 構造で守ること |
|---|---|---|
| `quality_check_records` | 検査の 1 セット（`check_key` 単位）の結果 | 一意 `(episode_id, subject_sha256, rule_version, check_key, generation)`。sha は 64 桁 hex の CHECK。`expires_at > checked_at` かつ有効期間 24h 以内の CHECK（`MAX_REPORT_VALIDITY` と同値のリテラル。一致は契約テストで固定）。`verdict`・`gate`・`producer` の語彙 CHECK。UPDATE を拒否するトリガ（Postgres。追記専用） |
| `quality_usage_declarations` | 「その完成動画で BGM / 地図が使われたか」の宣言（`feature`・`used`） | 一意 `(episode_id, subject_sha256, feature)`。`used=true` は `evidence_ref` 必須、`used=false` は `evidence_ref` NULL（CHECK）。UPDATE 拒否トリガ |
| `quality_reports` | gate が組み立てた `QualityReport`（監査用。拒否も残す） | `report_json`（`QualityReport.to_dict()`）・`report_sha256`・`final_video_sha256`・`input_hash`・`rule_version`・`generated_at`・`expires_at`・`verdict`。UPDATE 拒否トリガ |
| `quality_manual_approvals` | REVIEW を人が承認した記録 | `report_id` FK（`quality_reports`）、`approver_kind` CHECK（`contracts.approval.ApproverKind` の値）、`approver_id` 非空、`final_video_sha256`、`approved_at`。UPDATE 拒否トリガ |

- 4 表とも `episode_id` は `episodes(id) ON DELETE CASCADE`。**既存の表・制約・索引・列には触れない**（CHECK も広げない）。
- 行は消さない・書き換えない。同じ key の再検査は `generation` を 1 つ上げた**新しい行**で、読む側は最新世代だけを使う。
- **構造で再利用を防ぐ**: (a) 読み出し API は `(episode_id, subject_sha256)` を必須引数にし、「episode の最新」を返す関数を持たない。
  (b) 取得した行は純関数 `select_fresh_check_sets`（`domain/quality/records.py`）が `input_hash`・規則版・時刻・期限・
  `check_key` と gate の対応を**もう一度**検証し、通らないものは「無い」と同じに扱う（SQL の絞り込みに依存しない二重化）。
  (c) 保存側も同じ `validate_check_set` を通らないと書けない（不正な行を DB に入れない）。

### 3. 必須検査の集合（`contracts/quality_records.py` が唯一の定義）

| `check_key` | 対応する gate（全てを含むこと） | 必須か |
|---|---|---|
| `technical_qa` | `VISUAL`・`AUDIO` | 常に必須 |
| `description_lint` | `METADATA` | 常に必須 |
| `bgm` | `MUSIC`・`LICENSE` | `bgm` が**使われた**とき必須。使われないときは「不使用」の宣言が必須 |
| `map` | `GEO` | `map` が**使われた**とき必須。使われないときは「不使用」の宣言が必須 |

- 1 セットが対応する gate をすべて含まない（例: `bgm` に `MUSIC` だけ）/ 対応外の gate を含む / `deterministic=False` の検査を含む
  セットは書けず、読んでも無効。
- 必須が欠ける（記録なし・期限切れ・別 hash・別規則版・producer が失敗）は、`QualityReason.REQUIRED_CHECK_MISSING` の
  決定論の REVIEW 検査として報告に載る（ADR-0073 §2。集計は REVIEW。**PASS にはならない**）。使用宣言が無いときは
  `USAGE_DECLARATION_MISSING`（同じく REVIEW）。
- **不使用の明示記録**: `quality_usage_declarations` の `used=false` 行（対象 hash と `input_hash` に結び付く）。
  あるとき報告にはその gate ごとに「宣言された不使用」の PASS 検査（`detail` は固定文）を載せる。宣言が無いことは不使用ではない。
  `used=true` なのに該当 `check_key` の有効なセットが無ければ REVIEW。
- 宣言の書き手は **render 工程**（BGM / 地図を使ったかを知っている唯一の工程）。本 ADR は書き込み口
  （`QualityRecordWriter.declare_usage`）と読み手だけを定める。render への呼び出しの差し込みは BGM / 地図の枝の統合時に行う
  （§7 の接続点）。それまで宣言は存在せず、ON にすると REVIEW で拒否される（意図した fail-closed）。
- 技術 QA は `technical_qa.checks` の検査名（`domain.render.qa` の定数）を `VISUAL`（映像・字幕・網羅・読み戻し）と
  `AUDIO`（音声の有無・codec・sample rate・channels・尺・voice 網羅）に分ける。分け方の表は 1 箇所
  （`domain/quality/technical_qa_checks.py`）。未知の検査名は `VISUAL` 側に寄せて落とさない。
  どちらかの側が 1 件も無い、または 1 件でも不合格なら対応する gate は BLOCK。

### 4. 誰がどの検査を作るか（producer）

| `check_key` | 作る場所 | 入力 |
|---|---|---|
| `technical_qa` | gate 時点（`QualityGateService`）。final_video JSON を読み、正準 hash が `subject_sha256` と一致することを確認してから導出 | JSON 内の `technical_qa` |
| `description_lint` | gate 時点。**upload 工程と同じ関数**（`domain/upload/metadata_builder.py`）で台本から description を組み、marker 行を除いて lint | 台本（final_video が指す `source_script`、sha 照合） |
| `bgm` / `map` | 他の枝が作る純関数を、`CheckSetProducer`（§7）で包んで gate 時点に呼ぶ。または render が `write_check_set` で直接書く | 各枝が決める |

### 5. 説明欄の入力生成器（INV-131）

`domain/upload/metadata_builder.py::build_upload_metadata(...)` が、upload 工程と gate の**共通の**唯一の組み立て口になる。
flag `AVP_UPLOAD_DESCRIPTION_COMPOSED` が OFF のときは従来の `build_youtube_metadata(...)` 呼び出しと同じ引数・同じ結果
（`composition=None`）。ON のときだけ台本から `DescriptionComposition` を作る（導入文 = hook、補足 = narration の全体。
入りきらない補足は `compose_description` が落とす。出典・クレジットは引数で渡す枠で、現時点の呼び出しは空）。
upload 工程は同じ関数を呼ぶだけに置き換える（flag OFF で出力が byte 同一であることをテストで固定）。
gate はこの関数の結果の `description` から marker 行（意図した内部 ID）だけを除いて `find_internal_ids` を掛ける。
組み立てが `DescriptionBlockedError` になれば BLOCK（`METADATA_LINT_FAILED`）。台本が読めなければセットを作らない（= 必須欠落で REVIEW）。

### 6. 状態遷移・Temporal

- Episode 状態・遷移表・`PipelineStage`・Activity の入出力 dataclass・workflow は**変えない**（ADR-0076 §2 のまま。
  REVIEW / BLOCK は拒否のみ）。Activity の本体が DB / object storage を読み書きしても replay には影響しない。
  replay 検証は CI の `Temporal replay`（`.github/workflows/ci.yml` の対象ファイル）で確認する。
- `QUALITY_RULE_VERSION` を `deterministic-1` → `deterministic-2` に上げる（必須検査の集合という新しい規則が入るため。
  旧規則版のレポート・記録は構造上数えられない）。

### 7. 他の枝との接続点（CheckSetProducer / QualityRecordWriter）

```python
# contracts/quality_records.py
class CheckKey(StrEnum): TECHNICAL_QA; DESCRIPTION_LINT; BGM; MAP
class UsageFeature(StrEnum): BGM; MAP
CHECK_KEY_GATES: Mapping[CheckKey, frozenset[QualityGate]]

# domain/quality/records.py（純粋）
@dataclass(frozen=True) class CheckSubject: episode_id; final_video_sha256; input_hash
@dataclass(frozen=True) class CheckSetDraft: key; checks: tuple[QualityCheck, ...]; producer; producer_version; evidence_ref
class CheckSetProducer(Protocol):
    key: CheckKey
    async def produce(self, subject: CheckSubject, context: ProducerContext, *, now: datetime) -> CheckSetDraft | None
class QualityRecordWriter(Protocol):
    async def write_check_set(self, subject, draft, *, checked_at, rule_version) -> CheckSetRecord
    async def declare_usage(self, subject, feature, *, used, declared_by, evidence_ref, declared_at) -> UsageDeclaration
```

- BGM の枝: `bgm_quality_checks(...) -> tuple[QualityCheck, ...]`（gate `MUSIC`/`LICENSE`）を `CheckSetProducer(key=BGM)` で包む。
  ライセンスの有効期限は時間で変わるので、**gate 時点に呼ぶ**のが望ましい。render が `used=true` と BGM 証跡の
  `evidence_ref` を `declare_usage(UsageFeature.BGM, ...)` で書く。使わなかった render は `used=false`。
- 地図の枝: `map_quality_checks(...)`（gate `GEO`）を `CheckSetProducer(key=MAP)` で包む。宣言は同様。
- `ProducerContext` は final_video / 台本を gate が読み込み済みのものとして渡す（producer が object storage を再実装しない）。
- 登録は `PipelineActivities(quality_producers=...)` と、API の `QualityGateService`。producer の例外・`None` は記録を作らない（= 必須欠落）。

### 8. 書き込みのタイミングと flag OFF

- `AVP_QUALITY_GATE_ENFORCE` が OFF のとき、gate・手動経路は**記録を読まず書かない**。サービスも組み立てない。
  したがって migration 0019 を適用しても、OFF の挙動は byte 単位で同じ。render からの宣言書き込み（§3）は
  接続時に「宣言書き込み失敗は render を失敗させない（ログのみ）」とする。宣言が欠けても fail-closed になるだけである。
- ON の gate は検査・宣言を**必要になった時点（gate）で**書く。書き込み失敗は握りつぶさず、そのセットを「無い」として扱う（REVIEW）。
  レポートの保存に失敗したら `REPORT_PRODUCTION_FAILED` で拒否する（監査が残らない許可は出さない）。

### 9. 手動承認（INV-129）

- `POST /episodes/{id}/upload` が ON で REVIEW（`review_approved`）になったとき、承認者を依存 `get_upload_approver`
  （`UploadApprover(kind: ApproverKind, approver_id: str) | None`）から得る。統合時（Phase 3 統合枝）に `apps/api/auth.py::get_upload_approver` として
  API 認証（ADR-0080 §B）へ結線した: 未認証 = `None`、owner トークン = `OWNER`、agent トークン = `AGENT_DELEGATED`。
- `None` は 403（`quality: approver_unknown`）。`kind` が `OWNER` 以外（`AGENT_DELEGATED` を含む）は 403
  （`quality: approver_not_owner`）。**エージェントの委任承認では REVIEW を解放できない**。
- 承認は `quality_manual_approvals` に（誰・種別・いつ・対象 hash・`report_id`）を書いてから workflow を起動する。
  書けなければ起動しない。BLOCK と作成失敗は従来どおり 409（承認では解放できない）。PASS / WARN は承認者を要しない。

#### 9a. 所有者の host CLI（2026-11-02。API 認証が無効の本番での唯一の REVIEW 解放経路）

`AVP_API_AUTH_MODE=disabled`（ADR-0080 追補）では `get_upload_approver(None)` が `None` で、API からは REVIEW を解放できない
（`approver_unknown`）。そこで `scripts/quality-approve.py <episode_id> --owner-approval OWNER-<yyyymmdd>-<id> [--execute]` を足す。

- **所有者本人の行為**。host で所有者が実行する。`AGENT-…:OWNER-…`（委任）・種別なし・形違いは `domain.approval.parse_approval_reference`
  の検証と種別（`OWNER` のみ）で拒否する（検証器を複製しない）。承認者は `UploadApprover(OWNER, "host-cli-<ref>")`。
- 既定は dry-run（`evaluate(manual=True)` の結果を出すだけ。承認行も workflow 起動も無い）。`--execute` で、投稿受領（`upload_receipt`）が
  無く、結果が REVIEW の解放可（`review_approved`）のときだけ、API と**同じ関数** `start_upload`（承認の記録 → `upload_workflow_id` での
  起動）を呼ぶ。BLOCK・記録欠落・例外・flag OFF・受領済みは何も書かず終了コード 2。承認を書けなければ起動しない。
- 公開範囲は private のまま。upload の挙動は変えない。API の分岐は変えない（再利用であって複製ではない）。

### 10. 設定キー・語彙と §8 の grep（実施した件数）

実装前に `grep -rn "<key>" apps/ workers/ domain/ infrastructure/ contracts/ docs/` を走らせた。

| key | 件数 | 分類 |
|---|---|---|
| `quality_check_records` / `quality_usage_declarations` / `quality_reports` / `quality_manual_approvals` | 0 / 0 / 0 / 0 | 既存の衝突なし。書き手 = migration 0019 + `infrastructure/db/models.py` + 専用 repository 1 本 |
| `check_key`（列名） | 3 | すべて無関係（`domain/render/plan.py` の `_check_keys`。関数名の部分一致） |
| `CheckKey` / `UsageFeature` / `approver_kind` / `get_upload_approver` / `required_check_missing` / `usage_declaration` | 0 | 新規。定義は `contracts/quality_records.py`（前 2 つ）・`contracts/quality.py`（理由コード）・`apps/api/dependencies.py`（依存） |
| `ApproverKind` | 1 | `contracts/approval.py`（本枝の最初のコミットが作った唯一の定義。ADR-0080 と同一の文面） |

実装後（`grep -rn` を再実行。ADR 本文・invariants を除く）:

| key | 書き手 | 読み手 | 無関係 |
|---|---|---|---|
| 4 つの表名 | migration 0019（凍結 literal）・`infrastructure/db/models.py`・`infrastructure/db/quality_repositories.py` | `quality_repositories.py` だけ | 0 |
| `CheckKey` / `UsageFeature` | 定義 `contracts/quality_records.py` のみ | `domain/quality/records.py`・`technical_qa_checks`/`service`/`wiring`・`models.py`・`quality_repositories.py` | 0 |
| `REQUIRED_CHECK_MISSING` / `USAGE_DECLARATION_MISSING` | 定義 `contracts/quality.py` のみ | `domain/quality/records.py` | 0 |
| `ApproverKind` | 定義 `contracts/approval.py` のみ | `domain/quality/approval.py`・`models.py`・`quality_repositories.py`・`infrastructure/quality/service.py` | 0 |
| `get_upload_approver` | 定義 `apps/api/auth.py`（統合時に移動） | `apps/api/routers/episodes.py` | 0 |
| `build_upload_metadata` | 定義 `domain/upload/metadata_builder.py` | `workers/upload/activities.py`・`infrastructure/quality/service.py` | 0 |

migration の語彙 literal（凍結値）と契約の enum の一致は `tests/contract/test_migration_0019_quality.py` が固定する。
`quality_manual_approvals` の列は `approved_verdict`（`'review'` 固定の CHECK）・`approver_kind`・`approver_id`・
`approved_at`・`report_id`・`final_video_sha256`。

## Alternatives

- **検査結果を新しい ArtifactType として保存**: `artifact_metadata.artifact_type` の CHECK 拡張が要り、release 走査が止める（ADR-0060）。
  既存表を触らない新規の表なら本番へ先に適用でき、旧コード（e3c9555）は新しい表を知らないので動き続ける。
- **render 入力 hash の逆算で「BGM 無し」を推定**: 再現に font hash・engine・スレッド数が要り、gate が render の内部を複製する。却下。
- **「記録が無ければ不使用」**: 無検査を PASS にする穴（INV-101 の核）。宣言の無い使用有無は REVIEW。
- **時間で失効しない記録**: ライセンス期限・規則の更新を取りこぼす。有効期間は `MAX_REPORT_VALIDITY` 以内に固定。
- **UPDATE で最新に書き換える**: 監査が残らず、別 hash の記録を使い回す事故の元。追記専用 + 世代。

## Consequences

- ON にすると、必須検査と宣言が揃った Episode だけが自動で通る。揃っていなければ REVIEW で止まり、手動は所有者の承認が要る。
- 旧コード（e3c9555）は新しい表を参照しないので、migration を先に適用しても動く（逆に旧コードへ戻すときも表は残るだけ）。
- downgrade は行が残っていれば拒否する（監査記録を黙って消さない / ADR-0057 と同じ）。
- 新しい INV-126〜132、機械検査は `docs/invariants.md`。テストの理由は `docs/testing/v3-phase3-quality.md`。

## 追補: TRUNCATE の拒否は 0020 に分ける（再レビュー R1）

0019 は DELETE / UPDATE を拒否するトリガまで。`BEFORE TRUNCATE` のトリガは release 走査器が `TRUNCATE` の語で破壊的と数え、
marker（`# contract-migration: approved ADR-NNNN`）の承認 ADR を**デプロイ済みの revision**から読むため、同じ release で
初めて載る ADR-0081 では承認にならない。走査器は弱めない。よって:

- **0020**（TRUNCATE 拒否のトリガ 4 つ）を、ADR-0081 が本番の revision に載った**後**の release で出す。そのとき marker
  `approved ADR-0081` が効く。
- **残余リスク**: 0020 が入るまで TRUNCATE で記録を消せる。現状のアプリ接続は `DATABASE_URL` の `avp`（compose.yaml）で、
  ADR-0055 Part 2 のとおり `avp` は superuser のまま・ロール分離（DML のみの `avp_app`）は**設計のみで未適用**。
  superuser は権限検査を迂回するので、新しい表にも TRUNCATE の権限がある（確認は文書と compose の読み取りだけ。
  本番 DB へは接続していない）。`avp_app` へ移した後は、DML の GRANT に TRUNCATE を含めない限り TRUNCATE は拒否される。
  どちらも未適用の間、TRUNCATE への防壁はアプリが TRUNCATE を発行しないこと（コード上 0 箇所）だけ。

## ON にする条件（所有者の判断。本 ADR は判断しない）

1. migration 0019 が本番に適用済み（expand-only。適用だけでは何も変わらない）。
2. worker が本 ADR の `QualityGateService` を結線したイメージで動いている（`run_worker`）。API も同様（`create_app` 本番用）。
3. render が使用宣言を書く — **ADR-0086 で結線済み**（ENFORCE が ON のとき render が宣言を書き、BGM・地図の producer が登録される）。ENFORCE を ON にする前に描画された動画は、再描画するまで REVIEW。
4. 認証（`get_upload_approver`）が結線されている（手動 REVIEW の解放に必要）— 統合枝で結線済み。本番では API の token ファイル（ADR-0080 §B）が要る。
5. 隔離環境の integration test（ON/OFF、stale/missing）が通っている。
