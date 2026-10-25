# リリース手順書（preflight → deploy → 事後検証 → rollback）

Status: **Proposed**（ADR-0050 / INV-65）。所有者が承認するまで、この手順は「案」である。

> **全ての手順は、所有者の明示的な承認の後にだけ実行する。** 承認が無いまま本番に触れない
> （AGENTS.md §9）。`[R]` は読み取り専用（承認前に実行してよい）、`[W]` は本番の状態を変える
> （承認が要る）。preflight 自体は `[R]` で、何も変更しない。

## 0. 前提

- 対象は 1 つの **40 桁 SHA**（`git rev-parse <ref>` で確定させ、ブランチ名・短縮形は使わない）
- deploy は専用 worktree（`.worktrees/<agent>-release-<tag>`）で、その SHA を checkout した状態で行う
  （`deploy-workers.sh` は `HEAD` から build する）
- 証跡は `release/` 配下の**コミットしないファイル**として置く（`release/` は `.gitignore` 済みなので、
  `deploy-workers.sh` の `git status --porcelain` の dirty 判定にも掛からない。
  `tests/contract/test_release_evidence_ignored.py` が検査）:
  `release/approvals/<sha>.json` / `release/evidence/<sha>-replay.json` / `release/records/<tag>-preflight.json` /
  バックアップ manifest
- **承認・証跡は自己申告であって、認証ではない。** preflight が守るのは「古い承認・別 SHA の承認・
  承認後に差し替えた preflight 出力の流用」という**取り違え**だけで、悪意ある偽造は防げない。
  より強い手段（所有者の鍵で署名した git tag を承認とする等）は所有者の判断事項（§8）
- バックアップは `scripts/backup.py`（ADR-0051、手順は `docs/operations/backup-restore.md`）が取り、manifest を出す。
  `restore_verified: true`（復旧演習済み）の集合を使う。preflight はまだ `restore_verified` を要求しない（§8）

## 1. deploy 前（全て `[R]`）

1. 変更が **expand-only** か自分で確認する（§6）。contract migration を含むなら、先に ADR を書き、
   migration に `# contract-migration: approved ADR-NNNN` を付け、所有者の承認を取る
2. CI が green（`gh run list --commit <sha>`）。preflight は check run `CI gate`（`contracts/ci.py`）が success かだけを見る
3. Temporal の replay 証跡を作る（staging の手順 `docs/operations/staging.md` §5）。結果を
   `release/evidence/<sha>-replay.json`: `{"sha": "<sha>", "result": "pass", "at": "<ISO8601 with TZ>"}`
   （`at` が未来、または 72 時間より古いと BLOCKED。上限は `--evidence-max-age-hours`、最大 168）
4. バックアップ manifest（`python scripts/backup.py run` の出力 `<root>/<backup_id>/manifest.json` を渡す。
   DB dump / MinIO / Temporal 各 24h 以内。`--backup-max-age-hours` は最大 720。形式の最小形）:
   `{"components": {"db_dump": {"created_at": "<ISO8601 with TZ>"}, "minio": {...}, "temporal": {...}}}`
5. **承認は二段階**（承認が preflight 出力に束縛される）:

   ```bash
   # (a) 承認以外が全て揃った状態で preflight を走らせ、出力を保存する。承認が無いので総合は UNKNOWN(3) が正常
   python scripts/release-preflight.py --target-sha <sha> --backup-manifest <path> --json \
     | tee release/records/<tag>-preflight.json ; echo "exit=${PIPESTATUS[0]}"
   # (b) 所有者がその出力（承認以外が全て PASS であること）を読み、承認記録を書く
   sha256sum release/records/<tag>-preflight.json     # -> report_sha256
   ```

   `release/approvals/<sha>.json`:
   `{"sha": "<sha>", "approver": "<name>", "approved_at": "<ISO8601 with TZ>", "report_sha256": "<上の値>"}`
   （**承認は SHA 単位**。別 SHA・未来日付・72 時間超・出力の差し替えは BLOCKED。`report_sha256` が無いと UNKNOWN）
6. 承認を付けて再実行する。これが deploy の可否の判定:

   ```bash
   python scripts/release-preflight.py --target-sha <sha> --backup-manifest <path> \
     --approved-report release/records/<tag>-preflight.json --json \
     | tee release/records/<tag>-preflight-ready.json ; echo "exit=${PIPESTATUS[0]}"
   ```

   | 総合 | 終了コード | 意味 | 次 |
   |---|---|---|---|
   | READY | 0 | 必須が全て PASS | 所有者に報告 → deploy の承認を得る |
   | BLOCKED | 2 | 確認できて、止めるべき事実がある | 原因を直す。**押し通さない** |
   | UNKNOWN | 3 | 確認できなかった項目、または使い方の誤り・想定外の失敗 | 証跡を揃える／届かない先を直して再実行 |

   各チェックの detail に、読みに行った DB・Temporal namespace が出る。staging / `_test` DB / `avp-staging` を
   指していれば `target_environment` が BLOCKED（設定は repo root の `.env`）。
   `--allow-non-ff` は分岐した SHA に出すときだけ（前の SHA へ戻すのは §4 の `--rollback`）。
   BLOCKED の `provider_reservations` は再送（二重課金）の恐れ: 予約を照合（ADR-0013）して 0 にしてから出す。

## 2. deploy 中（`[W]`。承認後）

既存の `make deploy-workers` をそのまま使う。**preflight は deploy を包まない**（判定を変更しない）。

```bash
make deploy-workers      # = with-maintenance-pause.sh --reason deploy-workers --ttl 45m -- scripts/deploy-workers.sh
```

- `with-maintenance-pause.sh` は Daily Schedule を maintenance pause にし、**コマンドが成功したときだけ**
  解除する。失敗・Ctrl-C・SIGTERM では**解除しない**（壊れた状態で自動生成を再開しないため。スクリプト冒頭の
  コメントが正。`daily-schedule-guard.md` §2 の表の「失敗時も unpause」は古い）
- 運用者の pause（印なし）が有効なら deploy は走るが解除しない
- 失敗したら: **勝手に `maintenance end` / `reconcile` を呼ばない**。`schedule-guard.py status` で状態を見て、
  原因を直すか rollback（§4）を決め、所有者の判断で解除する。TTL（45 分）が切れると watchdog が解除する点に注意し、
  長引くなら先に所有者へ報告する
- `deploy-workers.sh` は migrate を新イメージで走らせてから全サービスを作り直す。途中で止まったときの
  `deploy-state/<time>-pre-<rev>.txt`（直前の稼働状態）を rollback の根拠にする

## 3. deploy 後（`[R]`）

```bash
python scripts/release-preflight.py --verify-post-deploy --target-sha <sha> \
  --pre-report release/records/<tag>-preflight.json
```

`--pre-report` は §1.5(a) の出力（承認の `report_sha256` と同じもの）。target SHA・mode が違う、または総合が
BLOCKED の出力は受け付けない。承認が別の出力に束縛されていれば `approval_binding` が BLOCKED。

全コンテナの revision == target / 必須 task queue（compose の healthcheck の `--queue` 一覧）に poller /
Schedule の状態が preflight 時点の記録と同じ / alembic == head。READY(0) 以外は成功と報告しない。
UNKNOWN も「まだ成功と言えない」。その後 `python scripts/schedule-guard.py status`、
`scripts/workers-versions.sh` を目視確認し、リリース記録（§7）を書く。

## 4. rollback（コードだけを戻す）

**alembic downgrade に頼らない。** 戻すのは「前の SHA のイメージ」。

旧イメージの `migrate`（`alembic upgrade head`）は、DB が旧コードの知らない revision にいると
`Can't locate revision` で失敗する。そのため rollback では **migrate を走らせない**
（`SKIP_MIGRATE=1`、既定オフ。migrate コンテナは削除され、残りのサービスだけを作り直す）。

1. 前の SHA を `release/records/` と `deploy-state/` から確定する
2. 前の SHA の worktree で §1 と同じ二段階を `--rollback` 付きで行う（`--allow-non-ff` は不要。分岐していれば付ける）。
   `--rollback` では DB が target より先にいても `alembic` を判定する: target の head から本番の revision までの
   migration が**全て expand-only**（静的走査で contract が 0 件）であること、かつ承認記録に所有者が
   **旧コード互換**を書いていること。書いていなければ UNKNOWN、contract が含まれていれば BLOCKED（§5 へ）:

   ```json
   {"sha": "<sha>", "approver": "...", "approved_at": "...", "report_sha256": "...",
    "rollback": {"old_code_compatible": true, "revisions": ["<本番が先に進んでいる revision を全て>"]}}
   ```

   `revisions` が preflight の `rollback_ahead` と一致しないと BLOCKED（承認が別の migration を指している）
3. 所有者の承認 → `SKIP_MIGRATE=1 make deploy-workers`
4. 事後検証 `--verify-post-deploy --rollback --target-sha <sha> --pre-report <§1.5(a) の出力>`。
   alembic は「preflight 時点の revision のまま（migrate も downgrade も走っていない）」を確認する。
   これ以降、`docker compose up`（`migrate` が `service_completed_successfully` の依存）を素で実行しない
   （旧イメージの migrate は失敗する）。前進するときは新しい SHA を通常の手順で出す

## 5. contract migration が既に流れてしまった場合

旧コードがもう動かない可能性がある。**自動で戻さない**（downgrade・手書き SQL をしない）。

1. 自動生成を止めたまま（maintenance pause を解除しない）、書き込みを止める（DB スイッチ `paused` /
   `uploads_paused`、workers.md §6-7）
2. 被害範囲を `[R]` で確認（どのテーブル・列が消えたか、その後に書かれた行）
3. バックアップからの復旧（DB dump → 検証用 DB で復元して差分確認）を**所有者が決定**する。
   復旧後に失う書き込みと、未照合の provider 予約（再送リスク）は所有者が承知した上で進める
4. 事後に ADR/invariants を見直し、同じ contract を再発させない

## 6. expand / contract ルール

- 1 回の deploy で出す migration は **expand のみ**: 列・表・index の**追加**、`nullable=True` または
  `server_default` 付きの NOT NULL、CHECK 語彙の**拡張**
- **contract**（`drop_column` / `drop_table` / rename / `server_default` 無しの NOT NULL / 破壊的 SQL）は
  **別のリリース**で、旧コードが完全に消えた後に出す。preflight は `upgrade()` を静的に走査し、
  `# contract-migration: approved ADR-NNNN` が無ければ BLOCKED。印が有効なのは、コメントとして書かれ、
  ADR が**deploy 済みの revision に在り**（その ADR を先に別 PR で入れて deploy する）、`## Status` が Accepted で、
  migration の revision id に言及しているとき。読めない書き方は UNKNOWN、本番に入った migration の書き換えは BLOCKED
- CHECK 語彙の拡張（`drop_constraint` + 再作成）も走査上は contract なので印が要る。
  **例外**（ADR-0060 / INV-82）: migration が `CHECK_WIDENING` を宣言し（table / constraint / column / old / new、
  `old` が `new` の真の部分集合）、`upgrade()` が `with op.batch_alter_table(table) as b:` の中で
  `b.drop_constraint(name, type_="check")` → `b.create_check_constraint(name, "col IN ('..', ..)")` の 2 文だけで、
  IN の集合が `new` と一致するとき、その 1 組は印なしで PASS（detail に `CHECK widening` と出る）。
  形が 1 つでも違えば従来どおり BLOCKED。宣言の `old` が実際の旧制約と一致するかは CI の
  `tests/contract/test_migration_0017_check_widening.py` 型のテストで migration ごとに示す
- 新規 DB は enum から作られた CHECK を 0012 の時点で最新語彙で持つ（本番は旧語彙）。拡張 migration の検査は
  「prod 形から流す」テストで書く
- 走査は **best effort**（AST + SQL の語）。通過は安全の証明ではなく、最終判断は人のレビュー（ADR-0050）

## 7. リリース記録テンプレート（`docs/releases/<tag>.md`、deploy 後に別ブランチでコミット。`release/` は ignore なのでここに置く）

```markdown
# Release <tag>
- target SHA: <40hex>
- previous (production) SHA: <40hex>   deploy-state: deploy-state/<file>
- approver / approved_at: <name> / <ISO8601>   approval file: release/approvals/<sha>.json
- preflight: READY at <ISO8601>   report: release/records/<tag>-preflight.json
- migrations: <none | revisions>   classification: expand-only | contract (ADR-NNNN)
- backup manifest: <path>   oldest component age: <h>
- replay evidence: <path or CI run>
- schedule snapshot (before): <daily paused=?, watchdog paused=?>
- deploy: started <ISO8601> / finished <ISO8601> / result: success | failed(stage)
- post-deploy verify: <READY|BLOCKED|UNKNOWN> at <ISO8601>
- schedules after: <same | differences + who resolved>
- rollback: <not needed | SHA redeployed at ISO8601, reason>
- notes / deviations (e.g. --allow-non-ff, manual maintenance end by whom):
```

## 8. 未実装・既知の穴

- release preflight が backup の `restore_verified` を要求すること（backup/drill 自体は `scripts/backup.py`。ADR-0051）
- replay 証跡の自動生成（staging.md §5 は手順のみ）
- **承認の認証**: 承認ファイルは自己申告（取り違えの防止であって認証ではない）。署名した git tag を承認とする等の強い手段は所有者の判断
- `docs/operations/daily-schedule-guard.md` §2 の表が `with-maintenance-pause.sh` の現挙動と食い違う（別 PR で直す）
- `RealCollectors` は実環境で一度も実行していない（fake のみ）。初回の本番 preflight は所有者の立ち会いで行い、結果を記録する
- replay 証跡のカバレッジは INV-40 系のみ（Upload / Production / Render の履歴は無い）

### 独立レビューの残件（P2/P3。所有者の判断待ち。この PR では直さない）

| # | 内容 | 扱い・判断メモ |
|---|---|---|
| 14 | CI の外部 action（`actions/checkout@v4` 等）・コンテナ image が**タグ参照**で、commit SHA / digest に固定されていない（供給網リスク） | **意図的に未対応**。SHA 固定は更新運用（Dependabot 等）とセットで所有者が決める |
| 9 / 16 / 17 | レビューの P2/P3 の番号のみが修正依頼に含まれ、**本文が修正担当に渡っていない**ため内容を記載できない | レビュー原文から転記が必要（推測で書かない）。転記するまで未対応として扱う |

`--pre-report` / 承認の検証、migration 走査、承認の束縛は「取り違えの防止」であり、悪意ある偽造や走査をすり抜ける
書き方を完全には防げない（best effort）。最終判断は人。

## 復旧検証と本番サービス健全性の判定（INV-65 / INV-66）

- `backup_freshness`: manifest が新しくても `restore_verified: true`（復旧演習に合格）でなければ **UNKNOWN**。
  READY には復旧演習済みのバックアップが必要。
- `service_health`（新設・必須）: compose project に `unhealthy` の container が1つでもあれば **BLOCKED**。
  healthcheck の閾値を緩めて通すことはしない（ADR-0052）。docker が読めなければ UNKNOWN。
- `provider_reservations`: 未照合が1件でもあれば現状 **BLOCKED**（一律）。対象 Episode・provider・変更範囲に基づく
  リスクベース判定は未実装（P2）。`scripts/reconcile-reservations.py list` で状態を確認する（ADR-0053）。

## 未照合予約の release 限定 waiver（ADR-0062 / INV-85）

`provider_reservations` が BLOCKED のとき、所有者が照合できない 1 件に限り、台帳を書かずに release を通せる。
予約は `reserved` のまま・再送は禁止のまま。条件は 1 つでも欠けると BLOCKED（予算が明示的な 0、実行中の
workflow 0、`reserved` がちょうど 1 件、期限 72 時間以内）。`release/` は ignore なのでコミットされない。
予算は repo の `.env` の `FAL_MONTHLY_BUDGET_USD=0` を読む。シェルに同名の環境変数があって `.env` と
違うと UNKNOWN になる（compose は環境変数を優先するため。`unset FAL_MONTHLY_BUDGET_USD` してから実行する）。

`release/waivers/<sha>-reservations.json`（`--reservation-waiver PATH` で変更可）:

```json
{
  "kind": "release_reservation_waiver",
  "sha": "<40hex>",
  "reservation_id": "<provider_reservations.id>",
  "provider_request_id": "<provider_job_ref の request_id>",
  "reservation_fingerprint": "<下記>",
  "approver": "owner",
  "owner_approval": "OWNER-<yyyymmdd>-<id>",
  "approved_at": "<tz つき ISO>",
  "expires_at": "<approved_at から 72 時間以内>",
  "reason": "<なぜ課金なし・再送しないと判断したか>",
  "required_budget_usd": "0",
  "evidence": [{"path": "release/evidence/<file>", "sha256": "<sha256sum の値>"}]
}
```

- 証跡のハッシュ: `sha256sum release/evidence/<file>`
- 指紋: 一度 waiver を（指紋を仮の値にして）置いて preflight を実行すると、BLOCKED の `data`
  （`--json`）に `expected_reservation_fingerprint` が出る。行の内容（id・request id・費用・時刻）が
  自分の確認と一致することを見てから waiver に写す。計算は `domain.release_preflight.reservation_fingerprint`。
- 結果は `provider_reservations: WAIVED` と警告行。承認済みレポートに含めてよいのはこのチェックの WAIVED だけ。
