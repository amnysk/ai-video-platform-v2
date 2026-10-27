# ADR-0062: 未照合の provider 予約を、release に限り期限つき・1 件限りで waive する

## Status

Proposed (2026-10-26)。ADR-0050（release preflight / INV-65）の `provider_reservations` チェックだけを
拡張する。DB スキーマ・台帳・状態機械・migration は変えない。

## Context

`provider_reservations` に `reserved` の行が 1 件でもあると preflight は一律 BLOCKED になる（ADR-0013:
再送すると二重課金になりうる）。fal の予算は意図的に 0（ADR-0061）で、予算 0 の間は新しい有料 submit が
起きない。それでも、予算 0 になる前に dispatch された 1 件が未照合のまま残ると、この 1 件のために
コード修正の release が永久に止まる。所有者が fal 側を確認して「課金なし・再送しない」と判断できても、
それを preflight に伝える安全な手段が無い。台帳の行を手で `abandoned` にする案は、照合の根拠なしに
台帳を書き換える（INV-15 / INV-67 の破壊）ので採れない。

## Decision

preflight の `provider_reservations` に限り、所有者が書いた waiver ファイルがあれば `WAIVED` を返せる。
**台帳は一切書かない**（予約は `reserved` のまま、再送は禁止のまま）。waiver は次を全て満たすときだけ有効:
SHA 束縛、`reserved` がちょうど 1 件でその id・provider request id と行の指紋（sha256）が一致、
所有者承認参照（`OWNER-<yyyymmdd>-<id>`）、72 時間以内の期限、証跡ファイルの sha256、
予算設定が明示的な 0（`evaluate_budget` が `DISABLED`）、実行中の workflow が 0。
予算設定の出所は repo の `.env`（deploy が適用する値）。プロセス環境に `FAL_MONTHLY_BUDGET_USD` があって
`.env` と（strip・引用符を除いて）違う場合は、compose の補間で環境変数が勝ちうるためどちらが効くか確定できず、
collector が `CollectorUnavailable` を投げて UNKNOWN になる（READY にしない）。1 つでも欠ければ BLOCKED。
`WAIVED` は `WAIVABLE_CHECKS`（`provider_reservations` のみ）でだけ必須チェックを満たし、他のチェックの
`WAIVED` は総合 BLOCKED。承認済みレポートも `provider_reservations` の `WAIVED` だけを許す。
waiver は preflight の警告として出力に残り（owner 承認参照は指紋のみ）、deploy 後検証には持ち越さない。

## Alternatives

- **台帳の行を `abandoned` / `failed` にして BLOCKED を外す**: 照合なしの台帳書き換えで、再送を
  許す入口になる。採らない（INV-15 / INV-67）。
- **`--ignore-reservations` フラグ**: 対象・期限・証跡が無く、新しい予約が増えても通る。採らない。
- **予約件数のしきい値**: 「1 件なら可」は別の 1 件に化けても通る。行の id・request id・指紋で束縛する。
- **`reconcile-reservations.py` で手動照合して `reserved` を消す**: 本来の経路で、可能なら最優先。
  ただし provider 側の証跡が得られない件はこの経路で閉じられないため、本 ADR は release の例外だけを扱う。

## Consequences

- 良い: 台帳を触らず、1 件・期限つき・所有者承認つきで release を通せる。新しい予約・予算の変更・
  実行中の有料作業があれば自動で BLOCKED に戻る。
- 悪い / 負債: waiver は自己申告のファイルで、認証ではない（`owner_approval` は形式検査。ADR-0050 の
  承認と同じ限界）。未照合予約は残り続けるので、waiver は照合の代わりにならない。
  collector が 2 つ増える（`pending_reservations`、`paid_budget_setting`。どちらも read-only）。
- 引き受けた前提: 予算 0 の間は新しい submit が無い（ADR-0061）。予算を正にするなら、先に照合する。
