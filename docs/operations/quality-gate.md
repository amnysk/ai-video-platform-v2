# 品質ゲート: 所有者が REVIEW を解放する（ADR-0081 §9a / INV-129）

品質ゲート（`AVP_QUALITY_GATE_ENFORCE=true`）が REVIEW を返した動画は、自動投稿されず、**所有者の承認**があるときだけ手動で投稿できる。
API 認証が無効（`AVP_API_AUTH_MODE=disabled`）の本番では API から承認できないため、**所有者が host で** CLI を実行する。
これは所有者の行為であり、エージェントが暗黙に実行する道ではない（エージェントの委任承認 `AGENT-…` は拒否される）。

## 手順

```bash
# 1. dry-run（既定）: 判定と理由を見るだけ。承認行も workflow 起動も無い
python scripts/quality-approve.py <episode_id> --owner-approval OWNER-<yyyymmdd>-<id>

# 2. 内容を確認し、所有者として承認するなら --execute
python scripts/quality-approve.py <episode_id> --owner-approval OWNER-<yyyymmdd>-<id> --execute
```

- `--owner-approval`: 所有者本人の参照 `OWNER-<yyyymmdd>-<id>`（認証ではなく形式検査。監査記録の `approver_id` に `host-cli-<参照>` で残る）。
- `--execute` 時、REVIEW が解放できるときだけ `quality_manual_approvals` に 1 行書き、API の手動投稿と同じ upload workflow
  （`episode-<id>-upload`）を起動する。動画は private で投稿される（契約。変わらない）。
- 次のときは何も書かず終了コード 2: BLOCK・検査記録の欠落・評価の例外・flag OFF・投稿受領（`upload_receipt`）が既にある・
  承認を記録できなかった・参照が委任/形違い。PASS / WARN は承認不要なので API の通常の手動投稿を使う。
- 本番の host で、`.env` の DB / Temporal / MinIO に接続できる環境から実行する。CI・テストからは実行しない。
