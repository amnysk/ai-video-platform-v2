# Storyboard Worker の運用

Status: **Accepted (2026-09-13)**（ADR-0015 / ADR-0016）

台本（`script`）から storyboard Artifact を作る worker。Codex CLI と OpenMontage checkout がホストに
あるため、**Ubuntu ホストのプロセス**として動かす（Temporal / PostgreSQL / MinIO は compose 側）。

## 起動

```bash
docker compose --profile core up -d --wait
./scripts/run-storyboard-worker.sh        # 別ターミナル
```

環境変数（未設定なら script が既定値を入れる）:

| 変数 | 既定 | 備考 |
|---|---|---|
| `OPENMONTAGE_REPO_PATH` | `<本体checkout>/ai-toolbox/repos/OpenMontage` | **読み取り専用**。未設定・非 git なら起動しない |
| `OPENMONTAGE_COMMIT` | `infrastructure/config.py` の固定値 | 仕様 blob は `git show <commit>:<path>` で読む。作業ツリーは読まない |
| `AI_VIDEO_WORK_ROOT` | `/mnt/minio-hdd/ai-video-work` | 一時作業領域（`work-directories.md`） |
| `STORYBOARD_TIMEOUT_SECONDS` | 900 | 生成1回の上限 |
| `CODEX_BINARY` / `CODEX_MODEL` | 台本 worker と同じ | |

## 実行・確認

```bash
curl -X POST http://localhost:8000/episodes/<episode_id>/storyboard   # 202
EPISODE_ID=<episode_id> ./scripts/smoke-storyboard.sh                 # 課金あり
```

`smoke-storyboard.sh` は storyboard_ready を待ち、MinIO から読み戻して契約・sha256・
`source_script.sha256 == 現行台本の sha256` を検査し、再POSTで job が `skipped`・同じ Artifact に
なることまで確認する。`EPISODE_ID` を省くと先に `smoke-script.sh` を走らせる。

## 状態と対処

| 症状 | 原因 | 対処 |
|---|---|---|
| POST は 202 だが何も起きない | Episode が `script_ready` / `storyboard_ready` に居ない（admit が拒否） | Episode の状態を確認。`blocked` からの再開は人間の判断 |
| Episode `blocked`、job `terminal_failed`、`StoryboardInputMissingError` | 現行の台本 Artifact が無い | 台本工程を再実行 |
| `StoryboardInputInvalidError` | 保存済み台本の sha256 不一致・契約違反 | MinIO / artifact_metadata を調査 |
| `UnreconciledReservationError` | dispatch 済みで生出力の無い `codex_storyboard` 予約が残っている | ADR-0013 の人手照合（呼ばない・消さない・解放しない） |
| `ArtifactConflictError`（readback mismatch） | 保存した storyboard を読み戻すと sha256 が合わない | ストレージ完全性の調査。未分類なので needs_input（INV-12） |
| ラウンドを使い切って `blocked` | 生成出力が毎回契約・カバレッジ違反 | プロンプト / 仕様の不整合を疑う（ADR-0014） |

再実行は冪等: 同じ台本・同じ生成器・同じ仕様なら `input_hash` が一致し、生成器を呼ばずに
既存 Artifact を返す（job は `skipped`）。台本が更新されると新しい storyboard 世代ができ、
古い世代は `superseded_at` が入る。
