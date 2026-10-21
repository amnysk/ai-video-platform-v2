# 未照合の provider 予約の照合（ADR-0053）

```bash
python scripts/reconcile-reservations.py list                 # 読み取り専用。JSON
python scripts/reconcile-reservations.py reconcile --observations-file obs.json   # dry-run
python scripts/reconcile-reservations.py resolve --reservation-id ID --outcome success \
    --evidence "<根拠>" --owner-approval "<token>" --actor <name>                   # dry-run
# 書くときだけ --apply（監査行を追記し、resolve は台帳を spent / abandoned にする）
```

- この CLI は provider へ接続しない。実 fal の status/result GET は **owner 承認が要る別手順**。
- `unknown` は失敗ではない。読めなかったことを理由に失敗扱い・再送をしない。
- 新しい idempotency key での再送は台帳が拒否する（INV-67）。`confirmed_success` は結果を取得する。
- preflight 用: `ProviderReconciliationRepository.unreconciled_summary()`（件数・provider 別・Episode 別・
  判定別・最古の経過秒・各件）。

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
