# 制作の測定（ADR-0035 §5）

```bash
python scripts/production-metrics.py                   # 全期間
python scripts/production-metrics.py --since 2026-09-29 # 適用日以降だけ
python scripts/production-metrics.py --json
```

**読み取り専用**: SELECT だけ。PostgreSQL ではトランザクションを `READ ONLY` にする。provider・
Temporal・MinIO には触れない。host の venv から `DATABASE_URL`（host から見た接続先）で実行する。
migration 0014（`provider_rejections`）より前の DB では失敗する（表が無い）。

| 項目 | 定義 | 材料 |
|---|---|---|
| provider 別の拒否率 | `provider_rejections` の件数 ÷ dispatch 済みの fal 予約数 | `provider_rejections` / `provider_reservations` |
| 再試行（ラウンド2以降の submit） | `round > 1` の dispatch 済み予約 | `provider_reservations` |
| 代替映像案の回数 | `scene_visual_override` Artifact の件数（と Episode 数） | `artifact_metadata` |
| 完成率 | `uploaded` の Episode ÷ 期間内の Episode | `episodes` |
| 1本あたり費用 | fal 予約の `estimated_cost_usd` 合計 ÷ Episode（全体・uploaded のみ） | `provider_reservations` |

注意:

- 費用は**見積り**（`estimated_cost_usd`）。fal の請求額ではない。拒否された試行は課金された前提で
  数える（拒否時の課金有無は provider が文書化していない）
- 拒否率の分母は dispatch 済みの予約。拒否画像ゲート・再送禁止で止めた submit は予約を作らないので
  分母にも分子にも入らない
- 構造化される前の拒否（2026-09-26/27 の2件）は migration 0014 が `error_summary` から一度だけ補完
  した行として数える

## 拒否率を確かめる小規模な検証の見方

適用日を `--since` に渡し、1〜2週間ごとに `fal_video` の拒否率・代替案の回数・完成率・
`per_uploaded_episode` を記録する。代替案が上限（INV-34）で止まった Episode は `blocked` として
完成率を下げるので、`blocked` の Episode は `jobs.error_summary` の `SceneAlternative*Error` で
理由を確認する。
