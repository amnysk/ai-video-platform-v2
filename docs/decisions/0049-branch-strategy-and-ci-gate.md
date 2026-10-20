# ADR-0049: ブランチ戦略（trunk-based・`main` = 本番）と単一の required check `CI gate`

## Status

Proposed（`main` の作成・default branch の変更・branch protection の適用は**所有者の承認待ち**。
本 ADR は方針と、承認前でも有効な CI 側の変更だけを確定する）

## Context

- GitHub の default branch は `claude/episode-vertical-slice`（Phase 1 時点で止まっている）。
  本番は `claude/daily-hardening`（revision 80775c0）から動いている。
- `ci.yml` は `push: branches: [main]` + `pull_request` だったが、`main` は存在しない。
  積み上げたブランチ（slice <- daily-hardening <- jpmas2-p0p1 <- agent-tools <- agent-studio-oss-design）
  への push では CI が走らず、PR を開くまで検査が無かった。
- branch protection は無い（API は 404）。required check が無いので、赤い CI のまま merge できる。
- 所有者1人 + AI エージェントの並行作業。CLAUDE.md §4/§5 は「1 agent = 1 branch = 1 worktree」「merge commit のみ・
  force push 禁止・main へ直接 push しない」を既に定めている。
- 本番は Temporal の走行中 workflow を持つ。デプロイ後に履歴が replay されるので、非決定になる変更は
  merge 前に止める必要がある（INV-40 の replay テストが存在する）。

## Decision

`main` を「デプロイ済みの本番」とする trunk-based（短命ブランチ + タグ `vYYYY.MM.DD.N`）を採り、
CI は全 PR と `main` / `develop` / `release/**` / `hotfix/**` への push で走らせ、
required check は集約 job **`CI gate`** ひとつにする。`develop` は常設しない。
`main` の初期位置（候補: 確認済み本番 SHA 80775c0）・default branch 変更・protection 適用は所有者の承認後。

## Alternatives

- **(A) GitFlow（main / develop / release / hotfix）**: 本番 = main、日々の統合 = develop。
  所有者1人では develop と main の二重管理が負債になり、back-merge の取り忘れ（hotfix が develop に
  入らない）が事故の型になる。エージェントの並行ブランチは develop に集まるだけで、衝突の解消先が
  増える。デプロイが1系統（Compose の本番ホスト）で、複数バージョンの同時保守が無い。不採用。
  ただし release 候補を実機で寝かせる必要が出たら `release/**` と `develop` を足せるよう、CI のトリガは先に開けておく。
- **default branch を現状のまま `claude/episode-vertical-slice` に保つ**: PR の既定 base と実際の本番がずれ続ける。不採用。
- **required check を job ごとに列挙する**: job を足すたびに protection を直す必要があり、直し忘れた job は
  無検査になる。`CI gate` に集約し、gate が全 job を `needs` に持つことを契約テストで固定する。

## Consequences

- 良い: 統合ブランチ・release・hotfix への push で必ず CI が走る。protection の required は `CI gate` 1つで済む。
  job 追加の入れ忘れは `tests/contract/test_ci_workflow_policy.py` が止める。Alembic の head 分岐・
  Workflow の非決定・secret/有料API/公開投稿の契約が CI の必須検査になる。
- 悪い（引き受ける負債）:
  - `main` 作成までは protection を掛けられず、現状の無保護状態が続く。
  - 積み上げ PR（#5 -> #6 など）は base が `main` でなくても CI が走る代わりに、`CI gate` が全部緑でないと
    required にできないため、重い Integration/Docker build が全 PR で走る。
  - `develop` を持たないので、複数エージェントの変更が `main` 上で初めて合流する。衝突は短命ブランチ側で
    merge commit により先に解消する（他人のブランチを rebase しない）。
  - 他ブランチ（agent-tools の ADR-0046 など）が `ci.yml` を変更しているため、統合時に `ci.yml` が衝突しうる。
    衝突時も `gate` の `needs` を全 job に保つ（契約テストが検出する）。
- 詳細・手順・protection の JSON は [docs/operations/branch-strategy.md](../operations/branch-strategy.md)。
