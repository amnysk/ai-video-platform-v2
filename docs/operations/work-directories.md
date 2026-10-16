# AI 動画生成の一時作業領域

Status: **Accepted (2026-09-13)**（ADR-0016）

`/mnt/minio-hdd` は独立した2つの領域に分ける。

- `/mnt/minio-hdd/minio-data` — **MinIO だけが管理する。** アプリコード・worker・OpenMontage・
  Codex・FFmpeg はこのパスを直接読み書きしない（検査: `tests/architecture/test_storage_boundaries.py`）
- `/mnt/minio-hdd/ai-video-work` — 生成・レンダーの中間ファイル用の一時作業領域（`AI_VIDEO_WORK_ROOT`）

正式な成果物の経路は変わらない:

```text
Worker -> ArtifactStore -> MinIO API -> MinIO -> /mnt/minio-hdd/minio-data
```

メタデータの source of truth は PostgreSQL（`artifact_metadata`）。

## 設定

`infrastructure/config.py::Settings`:

- `AI_VIDEO_WORK_ROOT`（既定 `/mnt/minio-hdd/ai-video-work`）— 絶対パスで、途中に symlink を含まないこと
- `OPENMONTAGE_REPO_PATH`（既定なし）— 共有 checkout。**読み取り専用**
- `OPENMONTAGE_COMMIT` — 仕様を読む固定 commit

## レイアウト

`infrastructure/workdir.py::WorkDirectory.create(episode_id, job_id)` が job ごとに作る:

```text
$AI_VIDEO_WORK_ROOT/
└── episodes/
    └── <episode-uuid>/
        └── <job-uuid>/
            ├── input/
            ├── output/
            ├── openmontage/
            └── tmp/
```

- ID は UUID に正規化する。UUID でない値は拒否する
- root が相対パス・symlink を含む・MinIO データディレクトリと重なる場合は拒否する
- 各構成要素を `lstat` で検査し、symlink を拒否する
- 失敗はすべて `WorkspaceUnavailableError`（`retryable`）

## 一時 vs 正式

一時ファイル（変換した入力、仕様のコピー、生成器の生出力の作業コピー、render の断片）は
**source of truth ではない**。job の成否にかかわらず削除してよい。

正式な成果物（script / storyboard / 画像 / 音声 / 動画）は必ず ArtifactStore 経由で MinIO に書き、
PostgreSQL に記録する。オブジェクトキーだけを見て現行版を推測しない。

## 削除

`WorkDirectory.cleanup(episode_id, job_id)` は job ディレクトリだけを削除する。
パスを `resolve()` しない。構成要素のどれかが symlink なら削除せず拒否する
（root 内の別 job への symlink を辿って他人の作業領域を消さないため）。

### storyboard 工程でのライフサイクル（ADR-0016 Decision 7〜9）

1. `prepare`（予約より前）: 作業領域を作り、変換した台本と仕様のコピーを `input/` `openmontage/` に書く。
   失敗しても予約台帳には何も残らない
2. `generate`: 生出力の作業コピーを `output/scene_plan.raw.txt` に書く（写しの失敗は警告だけ）
3. 生出力を MinIO（`provider-raw/`）に保存
4. `release`: 作業領域を削除。削除の失敗は警告ログだけで job を失敗にしない

`release` は「生成器が有料出力を返していない」か「生出力を MinIO に保存し終えた」ときだけ呼ぶ。
**生成器が戻った後で MinIO への保存に失敗した場合は作業領域を残す**（警告ログ
`keeping storyboard work directory` に episode / job / round が出る）。そのときは
`output/scene_plan.raw.txt` が有料出力の唯一の写しなので、照合（ADR-0013）が済むまで消さないこと。

保持期間・GC デーモン・容量管理は別の作業。
