# AGENTS.md — 開発ルール

このrepoは**AIエージェントが継続的に開発すること**を前提に設計されている。
人間もエージェントも同じルールに従う。

## 0. 変更前に必ず読むドキュメント

順序も守ること。

1. [docs/invariants.md](./docs/invariants.md) — **契約。最優先**
2. [docs/architecture/overview.md](./docs/architecture/overview.md)
3. 触る領域の設計書
   - Episode/Job/Artifactに触る → [docs/domain/](./docs/domain/) 該当ファイル
   - 状態遷移に触る → [docs/domain/state-transitions.md](./docs/domain/state-transitions.md)
   - 失敗処理・retryに触る → [docs/failure-policy.md](./docs/failure-policy.md)
   - コンポーネント境界に触る → [docs/architecture/components.md](./docs/architecture/components.md)
4. 技術選定の理由が関わるなら [docs/decisions/](./docs/decisions/) の該当ADR

**設計書に書いてある ≠ 実装されている。** 契約に依存する前に
`grep -rn "<不変条件ID>" tests/` でそれを強制するテストの存在を確認する。
テストの無い契約は願望であり、その上に別の設計を積んではならない。

## 1. TDDを基本とする

1. 失敗するテストを先に書く
2. 通す最小の実装を書く
3. リファクタする

例外は「実行可能なコードを含まない変更」（ドキュメント、設定コメント）だけ。
新しい不変条件を足すときは、それを強制するテストを**同じコミットで**書く。

## 2. 既存テストを安易に変更しない

テストが落ちたときの既定の対処は**実装を直すこと**。
テストを変更してよいのは次の場合だけで、いずれもコミットメッセージに理由を書く。

- 仕様変更がADR/設計変更として先に承認されている
- テスト自体のバグを実データで証明できる

「テストを緩めて緑にする」は禁止。落ちる契約テストは設計の食い違いの通報である。

## 3. unrelated file を変更しない

- 1つのブランチは1つの目的だけを扱う
- ついでのフォーマット修正・import整理・タイポ修正を混ぜない
- 別の問題を見つけたら、直さずに `docs/` かissueに記録して自分のタスクへ戻る

理由: 差分が大きいほどレビューは表層的になり、このrepoの事故はいつも
「レビューされなかった片側」で起きる。

## 4. Git 規律

- **force push 禁止**（`--force`、`--force-with-lease` とも）
- **他エージェントのcommitを reset / rebase / amend / rewrite しない**
- `main` へ直接pushしない。必ずPR
- merge commitで統合する。他人のブランチをrebaseしない
- コミットは小さく、1コミット1意図

## 5. 1 agent = 1 branch = 1 worktree

同時に走るエージェントは互いのチェックアウトを共有しない。

```bash
git worktree add .worktrees/<agent>-<topic> -b <agent>/<topic>
```

- ブランチ名は `<agent>/<topic>`
- 作業が終わったらworktreeを畳む
- 他のworktreeのファイルを読むのは可、書くのは禁止

## 6. architecture / invariant / contract を変えるときは先に設計

次のいずれかに触れる変更は、**コードより先に**ADRまたは設計書更新を行い、
同じPRに含める。

- `docs/invariants.md` の不変条件（追加・変更・廃止）
- コンポーネント間の呼び出し方向（誰が誰を呼ぶか）
- `contracts/` のスキーマ（後方互換を壊す変更は特に）
- 状態機械の状態値・遷移
- 技術選定（新しいミドルウェア・外部サービスの追加）

ADRの書式は [docs/decisions/README.md](./docs/decisions/README.md)。

**不変条件の番号は再利用しない。** 廃止した条件は削除せず
「廃止（日付・理由）」を残す。

## 7. 挙動を変えたら設計書を同じコミットで更新する

コードだけ / 設計書だけの片側コミットを作らない。
片側だけ更新することが、前身repoの事故の原型そのものだった。

## 8. 定義は1箇所に寄せる

2箇所以上が一致していなければならない値・列挙・優先順位は
`contracts/` か `domain/` に**1つだけ**置き、両側からimportする。
別モジュールで同じ集合をliteralに書き直さない。
新しい状態値・設定キーを増やすときは、先に

```bash
grep -rn "<キー名>" apps/ workers/ domain/ infrastructure/ contracts/ docs/
```

を**実際に走らせ**、ヒットを「読み手／書き手／無関係」に分類して設計書に件数付きで書く。

## 9. お金と外部副作用

まだ実装は無いが、方針は前身repoから引き継ぐ。

- 有料API（fal.ai等）の実呼び出しはテスト・CIから絶対に行わない
- YouTubeへの投稿は **private のみ**。public/unlisted への自動切替はしない
- 既存の投稿済み動画の変更・削除・再投稿をしない
- secret（APIキー、OAuthトークン）をログ・成果物・コミットに出さない
- provider予約が未照合のまま再送しない（二重課金防止）

これらはADRでも緩めない。所有者の明示的な判断だけが変えられる。

## 10. 提出前チェック

```bash
ruff check . && ruff format --check .
pyright
pytest tests/unit tests/contract tests/architecture
```

integration test は Docker Compose が必要（`make up` 後に `pytest tests/integration`）。
