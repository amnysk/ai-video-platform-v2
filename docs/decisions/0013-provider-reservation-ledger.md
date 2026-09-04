# ADR-0013: 外部AI呼び出しの予約台帳と INV-15 の意味論

## Status

Accepted (2026-09-04)

## Context

INV-15 は「課金を伴う外部呼び出しは予約を先に永続化する。プロセスがクラッシュしても
未照合の予約が残り、**自動で再送も解放もしない**」と定める。
Phase 1 では対象コードが無く「未検査」だった。

Phase 2 で Codex を呼ぶ時点から、この不変条件は現実の問題になる。
前身 repo では同じ形で **未照合 ¥612 が宙吊り**になり、
「呼んだのか分からない」状態が運用を止めた。

問題の本質は、外部呼び出しの**直後**にプロセスが落ちたとき、
「呼んだ／呼んでいない」を後から**証拠で**判定できないことである。

## Decision

**`provider_reservations` テーブル（予約台帳）を新設する。**
`jobs` への列追加ではなく独立テーブルにする理由は、1つの Job が
複数ラウンド＝複数回の外部呼び出しを持ちうるためで、列追加だと上書きになり
「未照合の予約が消える」形をこちらが作ってしまう。

主な列: `idempotency_key`（UNIQUE）/ `episode_id` / `job_id` / `provider` /
`input_hash` / `round` / `status` / `raw_output_key` / `outcome_artifact_id` /
`reserved_at` / `dispatched_at` / `reconciled_at` / `reconciled_by`。

**書き込み順序**（この順序が INV-15 の実体）:

1. `idempotency_key` で既存予約を引く（再開判定はここだけ）
2. **予約を INSERT して commit**（`status=reserved`）
3. **`dispatched_at` を UPDATE して commit** ← subprocess を起動する直前
4. 外部呼び出し（生出力は逐次 `provider-raw/` へ）
5. **`status=spent` + `raw_output_key` を commit** ← パース・検証より**前**
6. パース → スキーマ検証 → Artifact 保存

**5 を 6 より前に置くのが肝**。検証で落ちても「呼んだ」事実は確定する。

**再開時の分岐**（`idempotency_key` で引いた行の状態から一意に決まる）:

| 行の状態 | 意味 | 動作 |
|---|---|---|
| 行なし | 未予約 | 予約して呼ぶ |
| `spent` + Artifact あり | 完了済み | **呼ばずに既存を返す**（INV-17） |
| `spent` + Artifact なし | 呼んだが検証で落ちた | このラウンドは終了。次ラウンドは workflow が判断 |
| `reserved` + 生出力あり | 呼び出し**後**に crash | evidence 照合して `spent` へ。**再送しない** |
| `reserved` + `dispatched_at` が NULL | 起動**前**に crash（呼んでいない証拠） | 同一キーで続行してよい |
| `reserved` + `dispatched_at` あり + 生出力なし | **曖昧** | `NeedsInputError` → Episode `blocked`。**呼ばない・消さない・解放しない** |

さらに **新しい予約を作る前に、同じ Episode + provider に
「evidence の無い `reserved`」が残っていないかを確認**する。
残っていれば新ラウンドを開始せず `blocked` にする。
これがラウンドを跨いだ二重呼び出しの最後の砦である。

### 保守的照合（統合テストが暴いた設計の緊張点への解）

上の表だけでは、**ラウンド1がタイムアウトすると次のラウンドが永久にブロックされる**。
「retryable な失敗は安全に retry される」という Phase 2 の要件と正面から衝突する。

解は「呼び出しが**戻ってきたか**」で分けることである:

| 状況 | 記録 | 次ラウンド |
|---|---|---|
| 呼び出しが戻った上で失敗（timeout / 非zero exit） | `spent` + `reconciled_by="conservative"` + `failure_class` | **進める** |
| 呼び出し後に成功 | `spent` + `reconciled_by="evidence"` + 生出力 | 進める |
| Worker が落ちて**何も記録できなかった** | `reserved` のまま（誰も書けない） | **ブロック**（人手照合） |

`conservative` は「課金されたかは不明だが、**課金された前提で**確定する」という
安全側の判断である。前身 repo が同じ問題に対して採った規則
（未照合は再送も自動解放もしないが、保守的に spent として確定して進む）と同じ形。

**そのキーの呼び出しを再送することは無い。** 次ラウンドは別の
`idempotency_key`（round が違う）による**新しい**呼び出しであり、
総量は `jobs.max_attempts` の retry 予算が縛る。

**未照合の解消は人手のみ。** 予約の状態遷移表に
「`reserved` から自動事象で出る辺」を**載せない**こと自体が、
「自動で解放しない」の機械的表現である。

## Alternatives

**(a) `jobs` に予約列を足す** — テーブルが増えない。しかし複数ラウンドで上書きされ、
過去の未照合予約が消える。INV-15 が守ろうとしているものを壊す。却下。

**(b) 呼び出し後に1回だけ書く（予約を作らない）** — 実装が単純。
しかし呼び出し直後の crash で「呼んだ証拠」が一切残らない。INV-15 の全否定。却下。

**(c) `reserved` を一律ブロック（evidence 照合をしない）** — 安全側で単純。
しかし起動前 crash（呼んでいないことが確実な場合）まで人手待ちになり、
運用が止まる。曖昧な窓を `dispatched_at` と生出力で数百ミリ秒まで縮めるほうがよい。却下。

**(d) `reserved` を一律再送** — 運用は止まらないが二重課金する。INV-15 違反。却下。

**(e) コスト列（`estimated_cost_jpy` 等）も同時に入れる** — Phase 3 の fal.ai で必要になる。
しかし Codex はサブスクリプション実行で per-call 課金が無く、**今は読み手がゼロ**。
読み手のいない列は足さない（本repoの原則）。fal.ai を入れる回に同じ台帳へ足す。却下。

## Consequences

**良い側**
- 「呼んだか分からない」状態が構造的に消える（`dispatched_at` + 生出力が evidence）
- 二重呼び出しの防止が `UNIQUE(idempotency_key)` として **DB制約**になり、
  アプリのバグで破れない
- INV-15 の「未検査」を閉じられる（機械検査は §機械検査 参照）
- 同じ台帳を Phase 3 の fal.ai、Phase 4 の upload（INV-14）が再利用できる

**悪い側 / 引き受けた負債**
- **未照合予約の解消経路が人手のみ**なので、放置すると Episode が `blocked` で溜まる。
  通報（stalled 検知）とセットで運用する必要がある。**通報の実装は Phase 2 に無い**
- テーブルと状態機械が1組増え、Phase 3 で provider が増えるたびに
  `ProviderCall` 語彙の更新が要る
- 生出力（`provider-raw/`）はスキーマ検証を通らないので Artifact ではない。
  この「Artifact ではない MinIO オブジェクト」という第2の種類を導入した
  （INV-9/INV-10 との関係は本ADRの §Decision で明示的に区別している）
- 曖昧窓はゼロではない。`dispatched_at` commit と subprocess 起動の間に落ちると
  「呼んでいないのに曖昧」と判定され、人手照合が要る（安全側の誤り）

## 機械検査

- `tests/unit/test_provider_reservations.py::test_reservation_is_visible_from_another_connection_before_the_generator_runs`
- `tests/unit/test_provider_reservations.py::test_crash_after_dispatch_leaves_the_reservation_unreconciled`
- `tests/unit/test_provider_reservations.py::test_unreconciled_reservation_is_never_resent`
- `tests/unit/test_provider_reservations.py::test_reservation_table_has_no_automatic_exit_from_reserved`
- `tests/unit/test_provider_reservations.py::test_failed_call_can_be_conservatively_reconciled_without_evidence`
- `tests/integration/test_script_workflow.py::test_transient_failure_retries_in_a_new_round_and_succeeds`
