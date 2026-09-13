# ADR-0016: Storyboard 生成は「固定した OpenMontage 仕様で誘導した Codex」で行う

## Status

Accepted (2026-09-13)

## Context

Phase 3 の storyboard（ADR-0015）を作る方法として OpenMontage
（`ai-toolbox/repos/OpenMontage`、AGPLv3）の scene_plan 工程を使いたい。調査で分かった事実:

- **OpenMontage には実行可能なパイプライン・CLI・API が無い。**
  `docs/ARCHITECTURE.md` L92-94「OpenMontage does not call LLM APIs at runtime. The coding assistant is the LLM」、
  `AGENT_GUIDE.md` L80「Python = tools + persistence」。公式の使い方は Codex 等のコーディング
  エージェントに skill（Markdown）を読ませる形（`CODEX.md`、README L131 / L671）
- scene_plan 工程の定義は `pipeline_defs/animated-explainer.yaml` L140-158
  （skill `pipelines/explainer/scene-director`、入力 `script`、`human_approval_default: true`）
- OpenMontage 内の検証は `schemas/artifacts/__init__.py::validate_artifact` → `jsonschema.validate`（draft 2020-12）
- 共有 checkout には**第三者の未コミット変更**があり、`scene_plan.schema.json` に必須
  `generation_duration_seconds` が足されている。作業ツリーを読むと仕様が黙って変わる
- OpenMontage の Python を import すると AGPL の適用範囲の問題が未解決のまま生じる

## Decision

**生成器は既存の Codex アダプタで、プロンプトに「commit を固定した OpenMontage の仕様 blob」を載せて誘導する。**

1. 仕様は `git -C <repo> show <commit>:<path>` で**固定 commit の blob だけ**を読む（読み取り専用）。
   既定 commit は `2fa571e39ad0632148dad77c7a2134f7e6fe0797`（設定 `OPENMONTAGE_COMMIT`）。
   対象 blob: `skills/pipelines/explainer/scene-director.md`、`schemas/artifacts/scene_plan.schema.json`、
   `schemas/artifacts/script.schema.json`。**作業ツリーの未コミット変更は固定によって無視される**
2. 読めない（commit / blob 不在、repo 不在）は `GenerationSpecUnavailableError`（`needs_input`）。
   人間が checkout か設定を直せば回復する
3. 仕様の同一性は内容から導く `generation_spec_id`（commit と blob の sha256）。`input_hash` に入る（ADR-0015）
4. **OpenMontage の Python は import しない。** 生成出力の検証は我々の依存 `jsonschema`
   （`pyproject.toml`、ADR-0009）で固定 schema JSON に対して行い、その後 platform 所有の
   `StoryboardArtifact` へ変換する
5. 共有 checkout は**読み取り専用**。書き込み・checkout・stash をしない
6. 生成中の中間ファイル（変換した入力、仕様のコピー、生出力）は `AI_VIDEO_WORK_ROOT` 配下の
   job 単位の一時ディレクトリに置く（`infrastructure/workdir.py`、[work-directories](../operations/work-directories.md)）。
   **source of truth ではない**。正式な成果物は ArtifactStore → MinIO と PostgreSQL だけ
7. 生成器ポート（`domain/storyboard/ports.py::StoryboardGenerator`）は4段に分ける:
   `prepare`（入力変換・schema 検証・作業領域の作成・監査用入力の書き出し。外部呼び出しをしない）、
   `generate`（プロンプト生成と有料呼び出し、生出力の作業コピー）、`interpret`（純粋なパース・検証・変換）、
   `release`（作業領域の削除。例外を投げずログに残す）。生出力を先に保存してから解釈し、
   解釈だけを再実行できるようにするため（ADR-0013 の順序）。
   **`prepare` は予約より前に呼ぶ。** 局所的に失敗しうる処理（作業領域・入力不備）を予約・dispatch の
   commit 後に置くと、呼んでいないのに「dispatch 済み・evidence 無し」の予約が残り人手照合を招くため
8. 時間軸の正規化（`domain/storyboard/normalize.py::normalize_timeline`）は**activity だけ**が適用する。
   ドメイン規則なので生成器の実装ごとに再実装・適用させない（`interpret` は正規化前の下書きを返す）
9. activity は `release` を次のときだけ呼ぶ: (a) 生成器が有料出力を返していない（準備失敗・呼び出し失敗・
   保存済み生出力からの再開・予約照合での中断を含む）、または (b) 生出力を ArtifactStore に保存し終えた後。
   生成器が戻った後で生出力の保存に失敗したときは**作業領域を残し**、パスを警告ログに出す
   （そこが有料出力の唯一の写しのため）

### 参照した Codex 実装（`codex/storage-workdir`、未コミット）からの採否

作業領域の実装はレビュー済みの参照実装を**再実装**した。

- **採用**: episode/job 単位のレイアウト（`episodes/<episode>/<job>/{input,output,openmontage,tmp}`）、
  MinIO データディレクトリをアプリコードから触らない境界、root 外への symlink の拒否、root 自体を消さない検査
- **不採用**
  - `render/` サブディレクトリ・空き容量 API・`get_*_dir` ゲッター: Phase 3 に呼び出し元が無い（使われない API を作らない）
  - `compose.yaml` への環境変数追加: storyboard worker はホストプロセスで、コンテナは作業領域を使わない
  - リポジトリ全体を `rglob` する境界テスト: docs や CI も走査し誤検知と遅さを招く。コード層だけに限定した
  - **cleanup の symlink バグ**: 参照実装は削除対象を `resolve()` してから root 配下か検査していた。
    job ディレクトリが root 内の**別 job への symlink** だと検査を通り、他 job の中身を消す。
    再実装では resolve せず各構成要素を `lstat` で検査し、symlink なら拒否する
  - 任意文字列の ID 許容: ID は UUID に正規化し、それ以外を拒否する（パス区切り・`..` を構造的に排除）

## Alternatives

**(a) OpenMontage の Python（`validate_artifact` 等）を import する** — 検証の実装が一致する。
AGPL の範囲が未解決で、かつ必要なのは JSON Schema 検証だけなので却下。

**(b) 共有 checkout の作業ツリーを直接読む** — 手軽。未コミットの第三者変更で仕様が黙って変わり、
`input_hash` も揺れない（内容が変わっても気付けない）ので却下。

**(c) OpenMontage を vendoring / submodule 化する** — 固定は確実。ライセンス上の配布問題を持ち込み、
リポジトリが肥大化する。commit 固定の `git show` で同じ再現性が得られるので却下。

**(d) OpenMontage を使わずプロンプトを自前で書く** — 依存が消える。scene-director skill の
演出知見（尺の配分、映像の多様性）を捨てることになる。仕様の取り込み口を固定 blob に限定すれば
依存のリスクは管理できるので、今回は誘導を採った。

**(e) OpenMontage の人間承認ゲート（`human_approval_default: true`）を再現する** — 承認は
platform の状態機械（`ready_for_review` / `approved`）が持つ責務であり、生成器の中に二重に作らない。
今回は再現せず負債とする。

## Consequences

**良い側**
- 生成器の仕様が commit で固定され、再現可能で `input_hash` に反映される
- OpenMontage のコードに実行時依存せず、ライセンスの論点を広げない
- 生出力を保存してから解釈するので、解釈器のバグ修正後に再課金なしで回復できる

**悪い側 / 引き受けた負債**
- **OpenMontage の承認ゲートは再現していない。** storyboard は人間の確認なしに `storyboard_ready` に至る
- 仕様の実行主体は LLM なので、skill の指示（checkpoint・web 検索・ツール呼び出し）を
  プロンプトで「無視せよ」と抑える必要があり、その遵守は検証（schema + カバレッジ）でしか担保できない
- OpenMontage のスキーマ（秒 float、scene type 語彙）から platform 契約（int ms、`StoryboardVisualKind`）
  への変換表をアダプタが持つ。upstream の語彙変更は commit を上げたときに初めて顕在化する
- ホスト上の共有 checkout と `git` 実行ファイルに依存する（コンテナからは動かない）
- 作業領域は原則として job の終わりに削除する（Decision 9）。失敗時のデバッグ材料は MinIO の生出力だけになる。
  例外は生出力の保存失敗で、そのとき残った作業領域の回収は運用者の手作業（GC は未実装）

## 陳腐化条件

- OpenMontage が実行可能なパイプライン / API を提供したとき → 誘導ではなく直接呼び出しを再評価
- AGPL の適用範囲について所有者の判断が出たとき → (a) / (c) を再評価
- 固定 commit を更新するとき（変換表と検証の再確認が必要。全 Episode の storyboard が再生成対象になる）
- platform に人間承認の工程が実装されたとき → storyboard を承認対象に含めるか決める
