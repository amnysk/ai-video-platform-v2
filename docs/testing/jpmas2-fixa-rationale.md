# JP-MAS-002 fix A: テスト設計の理由

ADR-0043 §Consequences の負債2件（台本の再利用キーに CreativeBrief の版が無い / 介入 resume が
`AgenticEpisodeWorkflow` を再起動しない）を解消した変更のテスト。

## 1. 台本の再利用キー（tests/unit/test_script_brief_revision.py）

- `test_no_brief_revision_keeps_the_hash_and_a_revision_changes_it`（INV-55）: `None` は `input_hash` を
  そのまま返す（plan のある / brief の無い Episode の値は不変）。版が違えば別の hash、同じ版なら同じ hash。
- `test_a_script_made_under_an_older_creative_brief_is_not_adopted`: brief 不変なら再利用（二重生成・二重課金
  しない）、brief の内容が変わったら古い台本を採用せず再生成する。実際の `generate_script` で検査する
  （定義だけでなく、reuse の判定が hash を使っていること）。
