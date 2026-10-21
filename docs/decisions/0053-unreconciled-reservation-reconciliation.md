# ADR-0053: 未照合予約の照合記録・手動確定・再送ガード

## Status

Proposed (2026-10-21)

## Context

ADR-0013 は「未照合の予約を自動で再送も解放もしない」と定めたが、**未照合を解消する手段**は
「人手のみ」で、その手段（CLI・監査記録）は実装されていなかった。結果として、本番には
2026-10-04 から `reserved` のまま残る fal.ai の予約が1件ある
（詳細は [operations/reservation-reconciliation.md](../operations/reservation-reconciliation.md)）。

- fal の queue API は `COMPLETED` を返しても成功とは限らず、結果 body を読むまで成否は不明
- 結果取得が HTTP 403（資格情報）で拒否されると、「課金されたか・成功したか」が分からないまま
  Episode が `blocked` になる。この状態を「失敗」と読んで再送すると二重課金になる
- release preflight は未照合件数（`status='reserved'`）しか見えず、どの provider・Episode・
  どれだけ古いかが分からない。変更範囲と無関係な件まで一律に止める／一律に通す、の二択になる

## Decision

**予約ごとの照合判定を、追記専用の別テーブル `provider_reservation_reconciliations` に残し、
判定は5値（`confirmed_success` / `confirmed_failed` / `still_running` / `cancelled` /
`unknown`）に限る。`unknown` は失敗ではない。**

1. 判定の進み方は純粋関数（`domain/provider/reconciliation.py::advance_verdict`）で、冪等。
   確定（success / failed / cancelled）は他の確定へ変わらず、`unknown` / `still_running`
   に戻らない。`unknown` の観測は既存の知識を消さない。
2. provider の観測（`record_observation`）は判定行を足すだけで、台帳の `status` を変えない。
   台帳を動かすのは人手確定（`resolve_manual`）だけで、`--evidence` と `--owner-approval` が必須。
   承認 token は保存せず指紋（sha256 先頭16桁）だけを残す。success / failure は `spent`
   （課金された前提）、cancelled は `abandoned`。
3. **再送ガード**: `ProviderReservationRepository.reserve` は、同じ（Episode・provider・シーン・
   `input_hash`）に `confirmed_success` の予約、または未照合で `unknown` / `still_running` の
   予約があれば、新しい idempotency key での予約を `ResubmitRejectedError` で拒否する。
   fal に冪等キーは無いので、台帳が最後の砦になる。`confirmed_failed` は従来どおり次ラウンド可。
   **照合記録（判定行）が1つも無い `reserved` でも、`dispatched_at` が入っている（呼んだ可能性がある）
   なら `unknown` として扱い、同じく拒否する**（2026-10-21 独立レビュー P2-a。記録が無いことは無事の
   証拠ではない）。正確な規則（`blocks_new_round`）: 既存の別 idempotency key の行について、
   `confirmed_success` → 拒否 / status=`reserved` かつ（判定が `unknown`・`still_running`、または判定なしで
   `dispatched_at` あり）→ 拒否 / それ以外（`reserved` かつ未 dispatch = 呼んでいない証拠、`spent`、
   `abandoned`、`confirmed_failed`、`cancelled`）→ 従来どおり。同じ key は新規送信ではないので対象外。
   **並行制御**（P2-b）: `resolve_manual` は予約行を `SELECT ... FOR UPDATE` で読み、判定の読み直しと
   台帳の遷移をそのロックの下で行う（後から来た別 outcome は待った後に確定済み判定を読み、
   `ManualResolutionError` になって監査行を残さない）。`reserve` は入力タプルの
   `pg_advisory_xact_lock` を取ってから検査 → INSERT する（PostgreSQL のみ。SQLite は直列なので出さない）。
   advisory lock は検査と INSERT を直列化するだけで、未 dispatch の先行予約を拒否理由にはしない
   （それは `find_unreconciled` と ledger round の一意制約の担当）。実 PG での競合試験は
   `tests/integration/test_reservation_ledger_concurrency.py`（`TEST_DATABASE_URL` が無いと skip）。
4. `scripts/reconcile-reservations.py`（`list` / `reconcile` / `resolve`）。`reconcile` と
   `resolve` は既定 dry-run。**この CLI は provider へ接続しない**: status 関数は注入するか
   `--observations-file` で与える。実 fal への status/result GET は owner 承認が要る別手順。
5. release preflight 用に `ProviderReconciliationRepository.unreconciled_summary()`
   （件数・provider 別・Episode 別・判定別・最古の経過秒・各件）を提供する。**preflight への
   接続は本 ADR の範囲外**（risk-based 判定: 変更が provider 経路または同一 Episode に触れるなら
   BLOCKED、そうでなければ `unknown` / WARN は証跡に束縛した waiver が要る、という使い方を想定）。

## Alternatives

**(a) `provider_reservations` に verdict 列を足す** — 列が1つ増えるだけで簡単。しかし上書きで
履歴（誰がいつ何を根拠に確定したか）が消え、台帳の append-only 原則に反する。却下。

**(b) 観測で `reserved` を自動的に `spent` / `abandoned` にする** — 運用は楽。しかし
INV-15（`reserved` から自動事象で出る辺を載せない）を破る。観測は誤りうる（403 は失敗ではない）。却下。

**(c) 手動確定の evidence を任意にする** — 手順が軽い。しかし「緑にするための確定」を許す。
承認と根拠を必須にし、監査行に残す。却下。

**(d) ガードを `paid_job._plan_round` だけに置く** — 呼び出し経路が増えると破れる。
`reserve` に置けば全 provider・全経路が通る。却下。

## Consequences

**良い側**
- 「分からない」を失敗にも再送にも変えない状態が表現できる（`unknown`）
- 確定済みの課金を新 key で再送する経路が、台帳の書き込みで機械的に閉じる
- 手動確定が必ず根拠・承認の指紋・実行者つきで残る

**悪い側 / 引き受けた負債**
- 新テーブル1つ（migration 0016、expand-only）と `reservation.reconciled` イベントが増える
- ガードは `dispatched` な `reserved`（判定の有無を問わない）と `confirmed_success` に効く。
  未 dispatch の `reserved` は従来の経路（`find_unreconciled` / `_plan_round`）が扱う。
  呼び出し元（`paid_job`・planning・storyboard・scene_recovery・upload）は、いずれも
  `_plan_round`・`find_unreconciled`・`find_by_key`・`latest.status` の判定を通った後にだけ
  `reserve` するので、新規ガードで正規の経路が止まることはない（読んで確認、スイート全体で確認）。
- 実 fal の status/result を読む adapter は未実装（owner 承認が要るため意図的に外した）
- `confirmed_success` で `spent` にした予約は、結果を取得する再開経路（raw 出力の保存）が
  別途必要。現状は人手で扱う（ガードは再送だけを止める）
- 手動確定の `--owner-approval` は自己申告で認証ではない（INV-65 と同じ限界）
- 並行worktreeが同じ番号の migration を足すと single-head 検査で衝突する
