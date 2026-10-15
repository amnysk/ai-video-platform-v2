# JP-MAS-002 P1-D: 実環境 E2E と、そこで見つかった欠陥のテストの理由

P1 の単体試験は SQLite・time-skipping・fake の子 workflow で組まれていた。実 PostgreSQL + 実 Temporal + 実 MinIO
（隔離スタック `deploy/logging/test/run-e2e.sh`）で 1 Episode を通すと、SQLite では見えない欠陥が出る。

## 実環境で見つかった欠陥（単体）

| テスト | 守る契約・事故 | 捕まえる失敗 |
|---|---|---|
| `tests/unit/test_agent_reservation_key.py::test_the_stage_key_itself_does_not_fit_the_reservation_column` | 前提の固定: Activity の冪等キーは台帳の列（VARCHAR(64)）より長い | 前提が変わった（キーが短くなった）のに写しが残る |
| `tests/unit/test_agent_reservation_key.py::test_the_reservation_key_fits_the_column_and_is_deterministic_per_stage_key` | INV-56: 台帳の key は列に収まり、stage ごとに決定的で別の stage と衝突しない | 実 PostgreSQL で `StringDataRightTruncation`（全 agent stage が予約できず止まる）。SQLite は長さを見ない |
| `tests/unit/test_agent_reservation_key.py::test_a_reservation_for_a_real_sized_stage_key_is_found_again_by_the_mapped_key` | INV-56: 写した key でも再送は同じ予約（二重課金しない）で、`find_by_key` で引き直せる | 写しを片側（予約）だけに適用して再送が別予約になる |
