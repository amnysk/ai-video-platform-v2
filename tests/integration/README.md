# integration tests

Docker Compose の実サービス（PostgreSQL / MinIO / Temporal）に対して走る。
`pytest -m integration` でのみ実行される。

**有料provider（fal.ai）と実YouTube投稿には絶対に到達しないこと**（INV-18）。
これらは必ずfakeに差し替える。
