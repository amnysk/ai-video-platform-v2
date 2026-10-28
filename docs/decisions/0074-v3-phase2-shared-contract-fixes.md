# ADR-0074: v3 Phase 2 の共有契約の是正（verdict 統一・入力保護・品質レポートの鮮度）

## Status

Accepted (2026-10-29) — 契約と純粋 domain だけ。workflow / Activity / DB / migration には触れない。
予約帯: ADR-0074 / INV-105〜108（`docs/architecture/v3-phase0-audit.md` §1 の続き。本枝 `claude/v3-p2-contracts`）。
ADR-0071（geo）と ADR-0073（quality）の契約形を補う。両 ADR の INV-94 / INV-101〜104 の意味は変えない。

## Context

Wave 1 の統合後、Phase 2 の下流（MapRender・QualityGate 結線・BGM）が共有する契約に 3 つの弱点が残った。

1. 判定語彙 PASS/WARN/REVIEW/BLOCK が `contracts/geo.py::GeoVerdict` と
   `contracts/quality.py::QualityVerdict` に二重定義され、厳しさの順序も 2 箇所（`GEO_VERDICT_ORDER` /
   `VERDICT_SEVERITY`）にあった（AGENTS.md §8 違反）。
2. LLM 由来の文字列（地名・ラベル・主張）が検査結果やログに写り得る。
3. `QualityReport` は final_video と input_hash には結び付くが、どの規則版で・どのモデルで・いつまで有効か
   を持たず、規則を更新した後でも古い PASS が投稿を通せる。

§8 の事前 grep（`apps workers domain infrastructure contracts docs tests`、ファイル数）:

| 識別子 | 書き手（定義） | 読み手（import/使用） | 無関係 |
|---|---|---|---|
| `GeoVerdict` | 1（contracts/geo.py） | コード 3 + テスト 3 | docs 2 |
| `QualityVerdict` | 1（contracts/quality.py） | コード 3 + テスト 4 | docs 1 |
| `VERDICT_SEVERITY` | 1 | コード 1（domain/quality/aggregate.py） | docs 1 |
| `GEO_VERDICT_ORDER` / `stricter` | 1 | コード 3（geo/combine, geo/verify, quality 試験） + テスト 3 | docs 3 |
| `"pass"/"warn"/"review"/"block"` literal（上記 2 契約の外） | 0 | 0 | 3（`domain/release_preflight.py` / `domain/backup/manifest.py` の `"pass"` は別概念の結果文字列で、判定語彙ではない） |

## Decision

### 1. 判定語彙は `contracts/verdict.py` が唯一の定義（INV-105）

`Verdict`（StrEnum。値は従来どおり `pass`/`warn`/`review`/`block`）、`VERDICT_ORDER`（軽→重の唯一の順序）、
`VERDICT_SEVERITY`（順位。順序から導出）、`stricter(a, b)`、`worst(verdicts)`（空は PASS）。
`GeoVerdict = Verdict` / `QualityVerdict = Verdict` は**別名**（同一オブジェクト。サブクラスにしない）。
`GEO_VERDICT_ORDER` / `VERDICT_SEVERITY` / `stricter` は両契約から従来どおり import できる。
値・JSON・pydantic の直列化は変わらない。他モジュールが同じ語彙を literal で再定義することは
`tests/architecture/test_verdict_single_source.py` が禁じる。

### 2. 信頼できない文字列を結果・ログに残さない（INV-106）

監査（Wave 1 の d75cfa3 / 465cfc3 / a8270eb は tool id・例外文・uncertainty/名前の束縛を扱った。残りの穴）:

| 場所 | 穴 | 是正 |
|---|---|---|
| `domain/geo/verify.py` の `GeoCheck.detail` | `as_of` / `display_name` / `label.text` を `!r` で写していた（200 字・改行・偽 key をそのまま保存） | 写さない。`value_ref`（`untrusted_ref`: 長さ + sha256 先頭 12 桁）だけ |
| `GeoCheck` | 理由コード・対象・証拠が構造化されておらず `detail` の文頼み | `reason: GeoReason`・`target_id`・`evidence_ref`・`measures`（enum キー + 有限数値）・`value_ref` を追加。PASS ⇔ OK を強制 |
| `GeoCheck.name` / `detail` | 文字種の制約なし | `name` は `CHECK_NAME_PATTERN`、`detail` は印字可能 ASCII 500 字以内（改行・制御文字・非 ASCII 不可） |
| `QualityCheck.detail` | 無制限の自由文（LLM critic の出力がそのまま保存・取り込み可） | 同じ `detail` 規則を `__post_init__` で強制（取り込みは `REPORT_UNPARSABLE`）。値は例外文に写さない |
| `QualityCheck.scene_id` | 型だけで形式の検査なし | `SCRIPT_SCENE_ID_PATTERN` を強制 |
| `QualityCheck` | 証拠への参照が無い | `evidence_ref`（`SAFE_ID_PATTERN`）を追加 |
| OpenSearch での追跡 | 結果をログに載せる写像が無い | `GeoCheck.log_fields()` / `QualityCheck.log_fields()` が既存フィールド（`outcome` / `error_code` / `scene_id` / `attributes`。ADR-0040 の `LOG_FIELDS`）だけで返す。新フィールドは足さない |

語彙は `contracts/untrusted.py` に 1 箇所。`GeoCheck.reason` は互換のため `None` を許す（理由を持たない旧記録）が、
検証器が作る検査はすべて持つ（テストで強制）。音楽（`domain/music`）の拒否理由は `RejectReason`（enum）で既に構造化。
`CreditsError` は `track_id` を写すが `BGM_TRACK_ID_PATTERN` で字種は制限済み（**長さの上限が無い点は未対応**。
BGM 契約の担当で `max_length` を足すこと）。

### 3. 品質レポートの鮮度（INV-107）

`QualityReport` は次を持つ（`schema_version` を 1 → 2 に上げる。版 1 は鮮度を主張できないので
`REPORT_SCHEMA_MISMATCH` で拒否。まだ配線されておらず保存済みレポートは無い）:

| 要件 | フィールド | 備考 |
|---|---|---|
| 対象 artifact の sha256 | `final_video_sha256`（既存） | 再利用。`input_hash` も既存 |
| 規則・checker の版 | `rule_version`（新・必須） | 現行の版は呼び出し側が `current_rule_version` で渡す（版の値の定義は配線側） |
| AI の同一性 | `model: QualityModelIdentity \| None`（新） | `provider` + `model_id`（`SAFE_ID_PATTERN`）。`deterministic=False` の検査があれば必須（`REPORT_MODEL_IDENTITY_MISSING`） |
| 検査時刻（UTC aware） | `generated_at`（既存。名前は変えない） | naive は拒否 |
| 失効 | `expires_at`（新・必須・aware） | `generated_at < expires_at <= generated_at + MAX_REPORT_VALIDITY`（24h。`contracts/quality.py` の 1 箇所） |
| verdict・理由コード | `verdict` / `checks[].reason_code`（既存） | |

純粋関数 `domain.quality.freshness.check_freshness(report, *, current_final_video_sha256, current_input_hash,
current_rule_version, now) -> QualityReason | None` が、sha256 → 入力 hash → 規則版 → 時刻の形 → 未来日付
（`CLOCK_SKEW_TOLERANCE` まで許容）→ 失効（`now >= expires_at`）の順で、今のものに結び付かなければ理由コードを返す。
`evaluate_for_upload` は同じ関数を使い（判定の複製なし）、新たに `current_rule_version`（キーワード必須。省略可能にすると
fail-open になるため。ADR-0073 §3 の注記と同じ理由）を取る。時計は注入で、実時計は読まない。
追加の理由コード: `REPORT_RULE_VERSION_MISMATCH` / `REPORT_EXPIRED` / `REPORT_FRESHNESS_INVALID` /
`REPORT_MODEL_IDENTITY_MISSING`。INV-101 の拒否経路に「規則版違い・失効・鮮度の主張が不正・AI の同一性が無い」を足す
（意味は広がるだけで、既存の拒否は変えない）。ADR-0073 の `QualityReport` の列挙（§1）はこの節で拡張された。

## Alternatives

- **`GeoVerdict(Verdict)` のサブクラス**: StrEnum は値を持つと継承できず、別型になって `is` 比較が割れる。却下。
- **`detail` を自由文のままサニタイズ（赤塗り）する**: 取りこぼしが出る。そもそも入力を写さない設計にする。
- **`generated_at` を `checked_at` に改名**: 保存済みの語と取り込み側の全てが割れる。意味を docs に書いて維持。
- **`current_rule_version` を省略可能にする**: 省略した呼び出し側が規則更新後の古い PASS を通す（fail-open）。却下。
- **別名なしで全 import を書き換える**: 差分が大きく、他枝（未マージ）との衝突を生む。別名で互換を保つ。

## Consequences

- 判定語彙と順序が 1 箇所になる。下流は `contracts.verdict` を直接 import してよい。
- `GeoVerdict.__name__` は `Verdict` になる（repr / エラー文の表示だけが変わる）。
- `GeoCheck.detail` の文面が固定文になり、旧来の「地名入りの説明」は消える（追跡は `target_id` / `value_ref`）。
- LLM critic が日本語の自由文を `QualityCheck.detail` に入れる配線は、reason code + 固定文へ写像する責務が配線側に生じる。
- `evaluate_for_upload` の呼び出しは `current_rule_version` を渡す必要がある（配線前なので呼び出し元はテストだけ）。
- 失効は壁時計に依存する。時計の巻き戻し・ずれは `CLOCK_SKEW_TOLERANCE` と 24h 上限で縛るが、配線側の時計の信頼は別問題。
- 理由は `docs/testing/verdict-rationale.md`。
