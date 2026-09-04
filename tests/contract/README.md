# contract tests

`contracts/schemas/` のスキーマと、それを生成/消費する側の突き合わせ。

**生成側と取り込み側を同じテストの中で比較すること。** 片側だけのアサートは
前身repoで事故を1件も捕まえていない。

Phase 1 で最初に置くもの:
- `test_artifact_schema.py` — 全Artifactが schema_name/version を持つ（INV-10）
- `test_activity_payloads.py` — workflow が渡す型と activity が受ける型の一致
