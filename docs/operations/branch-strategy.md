# ブランチ戦略と CI ゲート運用

ADR-0049。**Status: Proposed。** 以下のうち「所有者承認が要る操作」（§9）は、承認があるまで実行しない。
このPRは CI の定義と文書だけを変え、`main` の作成・default branch の変更・branch protection の適用は行わない。

## 1. 現状（2026-10-20 時点の事実）

- default branch: `claude/episode-vertical-slice`（古い）。本番の系譜は `claude/daily-hardening`（80775c0）。
- `main` は存在しない。branch protection は無い。
- ブランチは線形: `slice <- daily-hardening <- jpmas2-p0p1 (PR#5) <- agent-tools <- agent-studio-oss-design (PR#6)`。

## 2. 比較

| 観点 | (A) GitFlow: main / develop / release / hotfix | (B) trunk-based: `main` = 本番 + 短命ブランチ + release タグ |
|---|---|---|
| 長命ブランチ | main + develop（+ release/hotfix） | main のみ |
| 所有者1人の管理コスト | develop と main の二重管理、back-merge 義務が常にある | 小さい |
| AI 並行エージェント | 全員の変更が develop で合流。衝突解消先が増える | 短命ブランチを main へ merge commit。1 agent = 1 branch と素直に対応 |
| 本番との対応 | main = 本番、だが develop は本番でない状態が長く続く | `main` = デプロイ済み。タグで版を固定 |
| Temporal replay 互換 | develop に未デプロイの変更が溜まり、replay 検査が「まとめて」になる | 変更ごとに PR の `Temporal replay` で検査。デプロイ単位が小さい |
| 同時に保守する版 | 複数版向き | 本番は1系統（Compose の1ホスト）なので不要 |
| hotfix | hotfix -> main と develop の両方へ | hotfix -> main のみ（develop が無い） |

**推奨: (B)。** 理由: 本番が1系統・所有者1人で、(A) の利点（複数版の同時保守、リリース候補の長期安定化）を
使わない。Temporal の走行中 workflow は非決定な変更で壊れるため、変更を小さく、1つずつ replay 検査して
デプロイするほうが安全で、develop に変更を溜める設計は逆に危険。

- `main` = **デプロイ済み（または、承認済みでデプロイ直前）の本番**。
- 作成位置は、**確認済みの本番 SHA（候補: 80775c0）**。**pending owner approval**: 所有者が本番の実 SHA を
  確認して承認するまで作らない。ここで SHA を最終決定しない。
- `develop` は**作らない**。作る条件: ① 実機で1日以上寝かせる未デプロイ変更が常態化し、② `main` を
  デプロイ可能に保てなくなったとき。そのときは ADR を足してから作る（CI のトリガは既に `develop` を含む）。
- `release/**` は、デプロイ前にリリース候補を固定したいときだけ短期に切る（`-rcN` タグを付ける）。

## 3. タグ

- 本番リリース: `vYYYY.MM.DD.N`（N はその日の通番、1 から）。例 `v2026.10.20.1`。
- リリース候補: `vYYYY.MM.DD.N-rcM`（M は 1 から）。
- タグは `main` 上の merge commit に付ける。付けたタグは動かさない・消さない（本番の戻し先の記録）。
- タグ付けは**デプロイ成功を確認してから**。

## 4. merge 方針

- merge commit のみ。squash / rebase merge は repo 設定で無効化する（§8）。
- force push 禁止。他人のブランチを rebase しない。main へ直接 push しない（CLAUDE.md §4）。
- PR は `CI gate` が success のときだけ merge する。

## 5. エージェントの作業ルール（CLAUDE.md §5 に従う）

- `git worktree add .worktrees/<agent>-<topic> -b <agent>/<topic> origin/main`（`main` 作成前は承認済みの基点を使う）。
- ブランチ名は `<agent>/<topic>`、1ブランチ1目的。他の worktree は読むだけで書かない。
- 作業が終わったら worktree を畳む。
- 積み上げ PR（他の未 merge ブランチの上）は base を直前のブランチにし、下が merge されたら `main` へ付け替える。
- DB migration を足すブランチは、base の Alembic head を確認する（`Alembic consistency` が head の分岐を落とす）。
- Workflow コードを変えるブランチは、`Temporal replay` が通ること。履歴 fixture を変えて通すのは禁止
  （CLAUDE.md §2。非決定になるなら `workflow.patched` で分岐する）。

## 6. hotfix フロー

1. `main` から `hotfix/<topic>` を切る（`origin/main` 基点）。
2. 失敗を再現するテストを先に書き（TDD）、最小の修正を入れる。
3. PR（base = `main`）。`CI gate` が緑になるまで merge しない。緊急でもスキップしない。
4. merge commit で `main` へ統合 -> デプロイ -> 動作確認 -> タグ `vYYYY.MM.DD.N`。
5. **back-merge**: `develop` を作っている場合のみ、`main` を `develop` へ merge commit で戻す。
   積み上げ中の未 merge ブランチ（例: PR#5, #6）には、`main` を merge commit で取り込む（rebase しない）。
   `develop` が無い間は、この積み上げブランチへの取り込みが back-merge に相当する。
6. `hotfix/**` ブランチは merge 後に削除。

## 7. 古いブランチの掃除（基準）

次を**すべて**満たすブランチだけを削除候補にする。削除自体も所有者の承認後。

- 先端が `main`（または本番タグ）に merge 済みである（`git branch -r --merged origin/main`）。
- 開いている PR が無い。
- 他のブランチ・worktree の base になっていない。
- 未 merge の唯一の成果（commit）を持たない。持つなら先に PR か `docs/` に記録する。

即時の削除はしない: `claude/episode-vertical-slice`、`claude/daily-hardening`（本番の系譜。`main` 作成と
タグ付けが済み、上の基準を満たしてから）。

## 8. branch protection（適用しない・payload の記録）

**これは適用しない。** 承認後、`CI gate` が一度 `main` 上で走って check 名が GitHub に登録されてから適用する
（未登録の check を required にすると merge が詰まる）。

`PUT /repos/amnysk/ai-video-platform-v2/branches/main/protection`（`gh api -X PUT ... --input protection.json`）:

```json
{
  "required_status_checks": { "strict": true, "contexts": ["CI gate"] },
  "enforce_admins": false,
  "required_pull_request_reviews": {
    "required_approving_review_count": 0,
    "dismiss_stale_reviews": false
  },
  "restrictions": null,
  "required_linear_history": false,
  "allow_force_pushes": false,
  "allow_deletions": false,
  "required_conversation_resolution": true
}
```

- `required_approving_review_count: 0`: 所有者1人では自己承認できない。PR 必須と CI 必須で直接 push を止める。
- `enforce_admins: false`: 緊急時に所有者だけが迂回できる逃げ道。使ったら事後に `docs/` へ記録する。
- merge 方式（repo 設定）: `PATCH /repos/amnysk/ai-video-platform-v2` に
  `{"allow_merge_commit": true, "allow_squash_merge": false, "allow_rebase_merge": false, "delete_branch_on_merge": true}`。
- `develop` を作った場合は同じ payload を `develop` にも適用する。

## 9. 移行計画（**すべて所有者の承認が前提**）

**`main` の作成と default branch の変更は、所有者の明示的な承認なしに行わない。**

0. 承認前（このPRで完了）: `ci.yml` の改修、契約テスト、本書と ADR-0049。
1. この PR（`claude/release-ci`）を `claude/daily-hardening` へ merge commit で統合する。
   以降、積み上げブランチへの push と PR で `CI gate` が走る。
2. 【承認】本番の実 SHA を所有者が確認する（候補 80775c0。pending owner approval）。
3. 【承認】`main` をその SHA に作成する。1. の CI 改修を取り込むため、`daily-hardening` を `main` へ PR（merge commit）。
4. 【承認】default branch を `main` に変更。§8 の protection と merge 方式を適用。
5. PR#5（jpmas2-p0p1）: base を `main` に付け替え、`CI gate`（特に `Alembic consistency` と `Temporal replay`）が
   緑なら merge commit で統合 -> デプロイ -> 確認 -> タグ。
6. PR#6（agent-studio-oss-design）: agent-tools を含むので、先に agent-tools の内容が #6 に含まれることを確認し、
   `main` を #6 のブランチへ merge commit で取り込んで CI を通してから統合。ADR 番号（0045-0048）と
   `ci.yml`（agent-tools の ADR-0046 が integration job を変更）の衝突を、統合時に解消する。ADR-0049 は欠番にしない。
7. 掃除（§7）は 6. の後。
