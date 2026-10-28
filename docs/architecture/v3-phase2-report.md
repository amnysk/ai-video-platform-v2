# v3 Phase 2 完了報告（ローカル統合・デプロイ判定）

- 作成日: 2026-10-29（JST）/ 本番実測: 2026-10-28 16:38Z・20:42Z（UTC、read-only）
- 統合ブランチ: `claude/v3-phase2-integration`（土台 `claude/v3-phase2-base` = 本番 303f33e + Wave 1）
- 監査: [v3-phase2-audit.md](./v3-phase2-audit.md)

## 1. 統合したブランチ

| ブランチ | 内容 | ADR / INV | 独立レビュー |
|---|---|---|---|
| `claude/v3-p2-audit` | 本番差分・意味的マージ監査・本番実測 | — | （文書のみ） |
| `claude/v3-p2-contracts` | Verdict 統一・未検証文字列の排除・QualityReport 鮮度 | ADR-0074 / INV-105〜107 | BLOCKER/MAJOR 0。MINOR 2 件は親が修正 |
| `claude/v3-p2-quality-gate` | `upload_gate` と手動投稿への品質ゲート結線（`AVP_QUALITY_GATE_ENFORCE` 既定 OFF） | ADR-0076 / INV-104 改訂 | BLOCKER/MAJOR 0 |
| `claude/v3-p2-youtube` | 照合 FOUND/NOT_FOUND/UNKNOWN、UNKNOWN で承認を消費、tags 単独照合の read-only 検証、説明欄構成（`AVP_UPLOAD_DESCRIPTION_COMPOSED` 既定 OFF） | ADR-0075 / INV-109〜112 | MAJOR 1 →修正→再レビューで閉鎖 |
| `claude/v3-p2-bgm` | BGM の Render 結線（`BGM_ENABLED` 既定 false）、ducking・ラウドネス・証跡 | ADR-0077（Proposed）/ INV-113〜116 | BLOCKER/MAJOR 0 |
| `claude/v3-p2-geo` | 地図の正確さ検査・見た目検査・決定論描画（`GEO_MAPS_ENABLED` 既定 false、未結線） | ADR-0078 / INV-117〜120 | MAJOR 2 →修正→再レビューで閉鎖 |

統合時の衝突は `docs/decisions/README.md` と `docs/invariants.md` だけ。INV が昇順になるよう節を並べ替え、
節記号を AC 契約 / AD 投稿 / AE BGM / AF 地図 とした（番号は不変）。

## 2. 検証（統合 HEAD）

- ruff check / format: 成功。pyright: 0 errors。
- `pytest tests/unit tests/contract tests/architecture`: 5803 passed。skip / xfail / deselect なし。
  Temporal replay（CI の replay 対象ファイル）を含む。
- integration test: **未実施**。本番が同じホストの compose（project `avp2`）で動いており、隔離環境を作れない。

## 3. 本番デプロイ判定: **BLOCKED**（本番は変更していない）

| # | 条件 | 判定 | 根拠 |
|---|---|---|---|
| 1 | Phase 2 の予定実装がすべて完了 | **FAIL** | 地図は未結線、QualityGate は検査入力（技術 QA・説明欄）を gate から読めず ON にすると全停止、LLM レビュー未実装（ADR-0076 §5）、説明欄の入力生成器なし |
| 2 | 独立レビュー完了 | PASS | 全 6 枝 |
| 3 | BLOCKER・MAJOR 0 | PASS | 修正後の再レビューで閉鎖 |
| 4 | 統合後の全件テスト | PASS | 5803 passed |
| 5 | pyright / ruff | PASS | |
| 6 | Temporal replay | PASS | Activity 名・入出力 dataclass・workflow は不変 |
| 7 | 稼働 SHA・DB revision の実測 | PASS | 303f33e / alembic 0018 |
| 8 | 既存の安全対策の維持 | PASS | 土台が本番 SHA。巻き戻しなし |
| 9 | fal 予算設定の維持 | PASS | 所有者判断で `FAL_MONTHLY_BUDGET_USD=60` を維持（変更しない） |
| 10 | private 投稿の維持 | PASS | 変更なし |
| 11 | 新 flag がすべて OFF | PASS | 本番 `.env` に未設定、compose は `${BGM_ENABLED:-false}` |
| 12 | DB 変更が後方互換 | PASS | migration なし。`reconciled_by` は CHECK の無い自由文字列 |
| 13 | バックアップ成功・復元検証 | **FAIL** | 最新 20261027T142232Z（30 時間超）。今回は取得・ドリルを実施していない |
| 14 | 既存 Workflow の安全な継続 | PASS（条件付き） | Running 0。ただし daily は 21:00Z 起動で、デプロイ時刻の調整が要る |
| 15 | 隔離環境での integration test | **FAIL** | 隔離環境なし |
| 16 | 対象サービスと影響の特定 | PASS | 全アプリ 14 コンテナ。`compose.yaml` 変更で render-worker の環境が変わる |
| 17 | 停止・復旧手順 | PASS | 既存 `--rollback`（303f33e）。DB 変更なし |
| 18 | 新規課金・投稿・Schedule 二重実行なし | **UNKNOWN** | デプロイ後のログ検証ができない（OpenSearch が 8 日前から停止、`avp-logging-check.service` failed） |

注: D-1（UNKNOWN で投稿しない・承認の消費）は flag の無い安全側の挙動変更で、デプロイすると即時に有効になる。

## 4. 未解決リスクと残作業

1. OpenSearch の復旧（所有者判断。今回は再起動していない）。
2. 隔離した staging（ADR-0050）での integration test。
3. 新しいバックアップと restore drill（ADR-0051）。
4. QualityGate の検査入力の保存先と migration 計画（後続 ADR）、手動承認の DB 記録、API 認証。
5. `QualityCheck.detail` の自由文（LLM レビューを有効にする前に allowlist 化）。
6. 地図の結線（`check_scene_map_accuracy` を使う、hash の結び付け、CJK フォント）、BGM のラウドネス閾値の校正、
   ADR-0077 の承認、BGM のラウドネス失敗時の扱い（現状はレンダ失敗）。
7. tags 単独照合の実機検証（所有者が `scripts/verify-tags-only-match.py --live` を実行）。旧動画は説明欄にしか marker が無い。
8. 既存 `tests/architecture/test_geo_boundaries.py` の import 検出の抜け（範囲外のため未修正）。

## 5. 追加コストの見込み

- 今回の変更による API 課金: 0（fal・YouTube・LLM の新規呼び出しなし。flag OFF）。
- flag を ON にした場合も、地図・BGM・品質ゲートは決定論で fal を呼ばない。LLM レビューは未実装のため 0。
