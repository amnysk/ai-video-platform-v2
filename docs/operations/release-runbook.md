# リリース手順書（preflight → deploy → 事後検証 → rollback）

Status: **Proposed**（ADR-0050 / INV-65）。所有者が承認するまで、この手順は「案」である。

> **全ての手順は、所有者の明示的な承認の後にだけ実行する。** 承認が無いまま本番に触れない
> （AGENTS.md §9）。`[R]` は読み取り専用（承認前に実行してよい）、`[W]` は本番の状態を変える
> （承認が要る）。preflight 自体は `[R]` で、何も変更しない。

## 0. 前提

- 対象は 1 つの **40 桁 SHA**（`git rev-parse <ref>` で確定させ、ブランチ名・短縮形は使わない）
- deploy は専用 worktree（`.worktrees/<agent>-release-<tag>`）で、その SHA を checkout した状態で行う
  （`deploy-workers.sh` は `HEAD` から build する）
- 証跡は `release/` 配下の**コミットしないファイル**として置く（`release/` は `.gitignore` 済みなので、`deploy-workers.sh` の `git status --porcelain` の dirty 判定にも掛からない。
  `tests/contract/test_release_evidence_ignored.py` が検査）:
  `release/approvals/<sha>.json` / `release/evidence/<sha>-replay.json` / バックアップ manifest
- **バックアップを取る自動手順はこの repo に無い**（未実装。§8）。manifest は所有者が取ったバックアップの記録

## 1. deploy 前（全て `[R]`）

1. 変更が **expand-only** か自分で確認する（§6）。contract migration を含むなら、先に ADR を書き、
   migration に `# contract-migration: approved ADR-NNNN` を付け、所有者の承認を取る
2. CI が green（`gh run list --commit <sha>`）
3. Temporal の replay 証跡を作る（staging の手順 `docs/operations/staging.md` §5）。結果を
   `release/evidence/<sha>-replay.json`: `{"sha": "<sha>", "result": "pass", "at": "<ISO8601>"}`
4. バックアップ manifest（DB dump / MinIO / Temporal 各 24h 以内）:
   `{"components": {"db_dump": {"created_at": "<ISO8601 with TZ>"}, "minio": {...}, "temporal": {...}}}`
5. 所有者の承認記録 `release/approvals/<sha>.json`: `{"sha": "<sha>", "approver": "<name>", "approved_at": "<ISO8601>"}`
   （**承認は SHA 単位**。別 SHA の承認は BLOCKED になる）
6. preflight を実行し、出力を保存する（deploy 後の比較に使う）:

   ```bash
   python scripts/release-preflight.py --target-sha <sha> --backup-manifest <path> --json \
     | tee release/records/<tag>-preflight.json ; echo "exit=${PIPESTATUS[0]}"
   ```

   | 総合 | 終了コード | 意味 | 次 |
   |---|---|---|---|
   | READY | 0 | 必須が全て PASS | 所有者に報告 → deploy の承認を得る |
   | BLOCKED | 2 | 確認できて、止めるべき事実がある | 原因を直す。**押し通さない** |
   | UNKNOWN | 3 | 確認できなかった項目がある（READY ではない） | 証跡を揃える／届かない先を直して再実行 |

   `--allow-non-ff` は rollback など「本番より古い SHA」に出すときだけ。BLOCKED の `provider_reservations`
   は再送（二重課金）の恐れ: 予約を照合（ADR-0013）して 0 にしてから出す。

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

全コンテナの revision == target / 必須 task queue（compose の healthcheck の `--queue` 一覧）に poller /
Schedule の状態が preflight 時点の記録と同じ / alembic == head。READY(0) 以外は成功と報告しない。
UNKNOWN も「まだ成功と言えない」。その後 `python scripts/schedule-guard.py status`、
`scripts/workers-versions.sh` を目視確認し、リリース記録（§7）を書く。

## 4. rollback（コードだけを戻す）

**alembic downgrade に頼らない。** 戻すのは「前の SHA のイメージ」。

1. 前の SHA を `release/records/` と `deploy-state/` から確定する
2. 前の SHA の worktree で §1 の preflight（`--allow-non-ff`）→ 所有者の承認 → `make deploy-workers`
3. 旧コードが新スキーマで動くことが前提。これが **expand/contract ルール**（§6）が守る性質
4. §3 の事後検証。ただし alembic は head より先にいるので `alembic_at_head` は BLOCKED になる。
   rollback の事後検証では「alembic が旧コードの知る revision の先にある」ことを所有者が記録して受け入れる
   （expand-only の間は、旧コードが新しい列を無視するだけで安全）

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
- CHECK 語彙の拡張（`drop_constraint` + 再作成）も走査上は contract なので印が要る
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

- バックアップを取る/検証する自動手順（manifest は手書き）
- replay 証跡の自動生成（staging.md §5 は手順のみ）
- CI の必須チェック名の固定（今は check-runs が全て success であること）
- `docs/operations/daily-schedule-guard.md` §2 の表が `with-maintenance-pause.sh` の現挙動と食い違う（別 PR で直す）
