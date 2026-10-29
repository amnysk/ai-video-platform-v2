# ADR-0080: 承認の種別（所有者本人 / エージェントへの委任）と API の認証

## Status

Accepted（v3 Phase 3 / Sonnet A。2026-10-29）。§A は migration なし。§B は API の既定を fail closed にするので、
デプロイ時に設定（トークン）が要る。

## Context

### A. 承認の種別
2026-10-29 の Phase 2 デプロイでは、所有者の条件付き指示（「安全条件をすべて満たした場合に限り、新機能 OFF で本番
デプロイまで実施」）を根拠に**エージェント**が release 承認ファイルを書いた（v3-phase2-report §3.4）。承認ファイルの
形式は `approver` が自由文字列で、所有者本人の承認と区別できない。同じ型の記録が他にもある（棚卸し）:

| 経路 | 場所 | 記録 |
|---|---|---|
| release 承認 | `release/approvals/<sha>.json` / `domain/release_preflight.py::_check_approval` | `approver` 自由文字列 |
| 予約 waiver | `release/waivers/<sha>-reservations.json` / `_waiver_identity` | `owner_approval` = `OWNER-…`（形式検査のみ） |
| breaker の半開・閉 | `scripts/provider-credits.py` / `provider_credits.validate_approval` | `provider_breakers.approved_by`（actor）・`approval_fingerprint`・`approval_evidence` |
| 保留の解放・再開 | 同上 `resume_episodes` | `provider_credit_holds.released_approval_fingerprint` |
| 予約の手動確定 | `scripts/reconcile-reservations.py` / `validate_manual_resolution` | `provider_reservation_reconciliations.actor`・`owner_approval_fingerprint` |

### B. API の認証
`POST /episodes`・`/storyboard`・`/production`・`/render`・`/upload`・`/resume`、`POST /research/...` は認証が無く、
API は `0.0.0.0:8000`（LAN 192.168.1.9）に公開されている（監査 v3-phase3-audit §4）。
`AVP_QUALITY_GATE_ENFORCE` が OFF（既定）だと手動投稿は品質ゲートを通らない。さらに、保留行を持たない古い `blocked`
Episode（本番に 15 件）は API の `/resume` で再開でき、月が替わって上限が戻ると課金が再開しうる。

## Decision

### A. 種別を述べる参照（DB 変更なし）

1. 語彙は `contracts/approval.py::ApproverKind`（`owner` / `agent_delegated`）の1箇所。
2. 承認の参照は種別を接頭辞で述べる: `OWNER-<yyyymmdd>-<id>`（所有者本人が**この操作そのもの**を承認）または
   `AGENT-<yyyymmdd>-<id>:OWNER-<yyyymmdd>-<id>`（エージェントが、後ろの所有者の条件付き指示の下で承認）。
   種別を述べない・日付が実在しない・委任元が無い参照は承認として読まない（fail closed）。**認証ではない**。
3. **所有者専用の関門**（`domain.approval.OwnerGate`）は委任では通さない:
   新機能の ON / fal 予算を上げる / public・unlisted 投稿 / provider 保留の解放・再開・breaker の半開と閉 /
   予約の手動確定（`failure` は再送→二重課金に、`success` は台帳を課金済みにする）。
   エージェントが単独で出来る委任の範囲は「新機能 OFF のコードデプロイ」と、その release 承認に限る。
4. release 承認記録は `approval_ref` が必須（無い・不正は UNKNOWN）。任意の `approver_kind` を書くなら参照の種別と一致すること。
   任意の `enables`（所有者専用の関門の名前の配列）に関門を書いた委任は BLOCKED。未知の名前は UNKNOWN。
   PASS の結果に `approver_kind` と、委任なら `delegated_by` を残す。
5. 予約 waiver・provider 資金操作・予約の手動確定は所有者本人の参照のみ（従来どおり `OWNER-…`）。
   保存は既存の文字列列に `<kind>:<name>`（`format_approver`）。種別の接頭辞が無い旧い値は
   `parse_approver` で種別 `None` = 所有者承認ではないと読む。名前が種別を偽装できない。
6. **migration は不要**。`claude/v3-p3-quality` が手動投稿の承認用に追加する DB 列は同じ `ApproverKind` を使う。

### B. 変更系ルートの認証

1. 変更系ルート（`POST`/`PUT`/`PATCH`/`DELETE` すべて）は `Authorization: Bearer <token>` を要求する。
   トークンは Settings の secret ファイル（`AVP_API_TOKENS_FILE`）か env から読む。比較は定数時間。
2. **未設定なら変更系を全拒否**（503。fail closed）。GET（読み取り）は開けたまま: 運用者・監視・healthcheck が
   使い、データの変更・課金・投稿を起こさないため。ただし内部 ID を返すので、公開を狭める勧告は別（compose の
   bind を 127.0.0.1 にする。この枝では compose を変えない）。
3. トークンは **actor を持つ**（`owner` / `agent`）。変更系の構造化ログに actor を残す（トークン値は出さない）。
   手動投稿の品質ゲートは、flag OFF の間は従来どおり。ON のとき手動投稿は BLOCK を迂回できない
   （REVIEW を許すのは owner トークンの actor だけ。agent トークンの REVIEW は拒否）。
4. 後方互換: 認証なしで API を呼んでいた運用スクリプトは 401/503 になる。呼び出し元の棚卸しと更新は
   v3-phase3-audit §4 に記す。

## Consequences

- release 承認の書式に `approval_ref` が増える（runbook 更新）。旧書式の承認ファイルは UNKNOWN（再発行が要る）。
- 保存された `approved_by` / `actor` は `owner:<name>` になる。表示・集計で旧い値（接頭辞なし）は「種別なし」と読む。
- API はトークン設定をしないと変更系が動かない（デプロイ時の設定が要る）。

## 影響を受ける不変条件

INV-123（承認の種別）、INV-124・125（API 認証。実装は同枝の後続コミット）。
