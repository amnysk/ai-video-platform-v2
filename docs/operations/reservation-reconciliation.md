# 未照合の provider 予約の照合（ADR-0053）

```bash
python scripts/reconcile-reservations.py list                 # 読み取り専用。JSON
python scripts/reconcile-reservations.py reconcile --observations-file obs.json   # dry-run
python scripts/reconcile-reservations.py resolve --reservation-id ID --outcome success \
    --evidence "<根拠>" --owner-approval "OWNER-<yyyymmdd>-<id>" --actor <name>                   # dry-run
# 書くときだけ --apply（監査行を追記し、resolve は台帳を spent / abandoned にする）
```

- `--owner-approval` は承認の参照番号 `OWNER-<yyyymmdd>-<id>`（実在する日付、id は英数字・`_`・`-` の
  6〜64 文字。例 `OWNER-20261021-TICKET42`）。**形式検査であって認証ではない**（誰でも書ける。承認の
  出所を後から辿るための参照を残させるだけ）。DB・ログには sha256 先頭16桁の指紋だけが残る。
- この CLI は provider へ接続しない。実 fal の status/result GET は **owner 承認が要る別手順**。
- `unknown` は失敗ではない。読めなかったことを理由に失敗扱い・再送をしない。
- 新しい idempotency key での再送は台帳が拒否する（INV-67）。`confirmed_success` は結果を取得する。
- preflight 用: `ProviderReconciliationRepository.unreconciled_summary()`（件数・provider 別・Episode 別・
  判定別・最古の経過秒・各件）。

## blocked な Episode を再開するときの安全な手順（コードを読んで確認、2026-10-21）

`reserved`（`unknown`）の予約を持つ Episode を再開しても、**fal へ再 submit する経路は無い**。根拠:

1. `POST /episodes/{id}/resume`（`apps/api/routers/episodes.py::_load_resume_plan` →
   `domain/pipeline/resume_plan.py::build_resume_plan`）は、`reserved` かつ `raw_output_key` 無しの予約が
   1件でもあると **409**（INV-15）。つまり未照合のままでは再開 API が拒否する。
2. 他の入口（工程ごとの POST・Temporal の再実行）が production に入っても、`PaidJobRunner.submit` は
   `_plan_round` で `reserved` + provider job 参照あり → `Submitted(newly_submitted=False)`（待つだけ。
   `generator.submit` は呼ばない）、dispatch 済みで参照なし → `UnreconciledReservationError`。
   待ちで fal の結果が再び 403 なら `ProviderUnavailableError` で `reserved` のまま（`spent` にしない）。
3. `resolve_manual` で `success` / `failure` にして `spent` になった後は、取得物が無いので
   `_plan_round` は次ラウンドを選ぶが、`reserve` の再送ガードが `confirmed_success` を拒否する
   （`ResubmitRejectedError`。INV-67）。`failure` は**意図して**次ラウンド（再生成 = 新たな課金）を許す。
   `cancelled` は `abandoned` になり同様に次ラウンド可。
4. 上記 1〜3 は `tests/unit/test_resume_unknown_reservation.py` / `test_resume_api.py` /
   `test_paid_job.py` が固定している。

手順（sb4 のような件）:
1. 運用者が fal の状態を確認する（owner 承認が要る別手順。このリポジトリの CLI は接続しない）。
2. 結果が取れ成功していた: `resolve --outcome success`（`spent` + 監査行）。**この後に再開しても、取得物
   （raw 出力）を台帳へ取り込む経路は無く、新 key での再送は拒否される**。結果を取り込むのは
   現状 **手動**（下記「未対応」）。
3. 失敗・課金なしと確認できた: `resolve --outcome failure`（保守的に `spent`）→ 再開すると次ラウンドで
   **新たに課金される**。この課金は owner が承知していること。
4. 確認できない（403 のまま）: 何もしない。再開 API は 409 のまま。`unknown` を失敗に読み替えない。

**未対応（人手のまま）**: `confirmed_success` にした予約の結果 video を fal から取得し raw 出力・Artifact
として保存する再開経路は無い（ガードは再送だけを止める）。また `JobFailed`（fal が失敗を返した、結果 body に
video url が無い等）は `_spend_conservatively` で `spent` になり、次の再開で次ラウンドが課金されうる。
これは provider が失敗を明示した場合の既存仕様（ADR-0017）で、`unknown` ではない。

## 2026-10-21 時点の本番の未照合予約（1 件、読み取りのみで確認）

| 項目 | 値 |
|---|---|
| reservation id | `591d1a8b-ab05-4d9b-b8f7-befad36cc2db`（provider `fal_video`、シーン `sb4`、round 1） |
| episode | `a8a207f4-7003-49b7-bafc-cfea171d6238`（`blocked`: `ProviderUnavailableError: fal result refused: HTTP 403 (credentials)`） |
| job | `b79d1e5b-768b-4052-9a05-c19087c2a6cb`（`produce_scene_video`、`terminal_failed`、attempts 1/3、`needs_input`） |
| fal request id | `01a108c3-af05-7fc0-b5cd-42d24f8105dc`（endpoint `bytedance/seedance-2.0/fast/image-to-video`） |
| reserved_at / dispatched_at | 2026-10-04 21:13:23.57Z / 21:13:23.59Z（経過 約 16 日） |
| 見積もり | USD 1.9352（確定額ではない） |
| raw_output_key / outcome_artifact | なし / なし（`sb4` の `scene_video` Artifact は存在しない） |
| 最後に観測した状態 | 台帳に観測は残っていない。コード上、result GET は status が `COMPLETED` の時だけ呼ばれる（`fal_seedance_video.py::poll`）ので、**キュー状態は COMPLETED、結果 body は 403 で未読**と推定 |
| 判定 | **UNKNOWN**（完了はしているが成功・失敗・課金額が未確認） |
| 同 Episode の他の `fal_video` | sb1・sb2・sb3 は `spent` + evidence + Artifact あり。sb4 だけ未照合 |

アプリログ: worker コンテナは 2026-10-06 に作り直されており、request id / reservation id の
ヒットは全 worker で 0 件。ログ用 OpenSearch（`avp2-logging-opensearch-1`）は停止中で未確認。

再照合に必要な呼び出し（**owner 承認が要る。ここでは実行していない**）:
`GET {status_url}`（`.../requests/<id>/status`）と、`COMPLETED` なら `GET {response_url}`
（`.../requests/<id>`）。どちらも提出済みジョブの取得で新たな生成は起こさない
が、fal 側の結果保持期間・資格情報の状態（403 の原因）は未確認。403 のままなら照合できない。
