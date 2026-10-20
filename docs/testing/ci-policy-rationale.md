# CI 方針の機械検査のテストの理由

ADR-0049。`ci.yml` は branch protection の required check の元になるので、片側（job だけ／gate だけ）の更新を
機械で止める。

- `tests/contract/test_ci_workflow_policy.py`
  - `permissions` が `contents: read` のみ: 既定の GITHUB_TOKEN は書き込み権限を持ちうる。
  - push が `main` / `develop` / `release/**` / `hotfix/**` を覆い、PR は base を絞らない: 以前は
    `push: [main]` で、`main` が実在しない間、統合ブランチへの push で CI が一度も走らなかった実害がある。
  - required check 名の安定: 名前の変更は branch protection の設定変更と同義。
  - `CI gate` の `needs` が他の全 job と一致: job を足して gate に入れ忘れると、その job は誰も見ない。
    失敗を先に確認した（gate 追加前は 5 件失敗）。
  - gate が `always()` で success 以外（skipped 含む）を落とす: `needs` が skipped のとき job 自体が
    スキップされ「緑」になる GitHub の挙動を避けるため。
  - 何もしない job（`true` のみ、`|| true`）の禁止。
- `tests/contract/test_migration_single_head.py`: 並行ブランチが migration の head を2本に割る事故を PR で検出する。
  既存 migration は1 head なので最初から緑（新規の失敗を伴わないガードで、head を2本にする変異で赤になる想定）。
