# Architecture Decision Records

architecture / invariant / contract に触れる変更は、コードより先にADRを書く
（[AGENTS.md §6](../../AGENTS.md)）。

## 書式

ファイル名: `NNNN-kebab-case-title.md`（連番、再利用しない）

必須節（順序固定）:

1. **Status** — `Proposed` / `Accepted` / `Superseded by ADR-NNNN` / `Deprecated`
2. **Context** — 何が問題か。実データ・実際に起きた事故を書く
3. **Decision** — 何を決めたか。1文で言い切る
4. **Alternatives** — 検討して**採らなかった**案と、採らなかった理由
5. **Consequences** — 良い結果・悪い結果の両方。特に**引き受けた負債**を明記

Alternatives と Consequences の悪い側を書いていないADRは未完成として扱う。

## 一覧

| ADR | タイトル | Status |
|---|---|---|
| [0001](./0001-adopt-temporal.md) | Temporalを採用する | Accepted |
| [0002](./0002-adopt-postgresql.md) | PostgreSQLをsource of truthにする | Accepted |
| [0003](./0003-adopt-minio.md) | MinIOでArtifact本体を保持する | Accepted |
| [0004](./0004-adopt-fastapi.md) | FastAPIをAPI層に採用する | Accepted |
| [0005](./0005-adopt-docker-compose.md) | Docker Composeでローカル環境を組む | Accepted |
