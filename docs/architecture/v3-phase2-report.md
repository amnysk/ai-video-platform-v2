# v3 Phase 2 完了報告（ローカル統合・デプロイ判定）

- 作成日: 2026-10-29（JST）/ 本番実測: 2026-10-28 16:38Z・20:42Z（UTC、read-only）
- 統合ブランチ: `claude/v3-phase2-integration`（土台 `claude/v3-phase2-base` = 本番 303f33e + Wave 1）
- 監査: [v3-phase2-audit.md](./v3-phase2-audit.md)
- 改訂: 2026-10-29 判定を「全機能完成（A）」と「flag OFF のコードデプロイ（B）」に分割。OpenSearch 停止維持の運用判断を追記（§3.3）

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

## 3. デプロイ判定（2 つを別々に判定する）

Phase 2 の**全機能完成**と、**新機能 OFF でのコードデプロイの安全性**は別の問いとして判定する（所有者指示 2026-10-29）。
前者が FAIL でも、後者の条件をすべて満たせば flag OFF のままコードだけを出してよい。後者の条件を 1 つでも欠けば出さない。

### 3.1 判定 A: Phase 2 の全機能完成 — **FAIL**（継続作業）

地図は未結線、QualityGate は検査入力（技術 QA・説明欄）を gate から読めず ON にすると全停止、LLM レビュー未実装
（ADR-0076 §5）、説明欄の入力生成器なし。§4 の残作業が閉じるまで新 flag は ON にしない。

### 3.2 判定 B: 新機能 OFF でのコードデプロイ

| # | 条件 | 判定 | 根拠 |
|---|---|---|---|
| B1 | 独立レビュー完了・BLOCKER/MAJOR 0 | PASS | 全 6 枝。修正後の再レビューで閉鎖 |
| B2 | 統合後の全件テスト・pyright・ruff | PASS | 5803 passed / 0 errors |
| B3 | Temporal replay（repo の履歴 fixture） | 証跡を target SHA で作る | `release/evidence/<sha>-replay.json`（staging.md §5） |
| B4 | CI gate（check run `CI gate`）が target SHA で success | 確認する | preflight `ci` |
| B5 | 稼働 SHA・DB revision・祖先関係 | PASS（実測） | 303f33e / alembic 0018、target は 303f33e の fast-forward |
| B6 | DB 変更が後方互換 | PASS | migration なし |
| B7 | fal 予算設定の維持 | PASS | 所有者判断で `FAL_MONTHLY_BUDGET_USD=60` を維持（変更しない） |
| B8 | private 投稿・投稿冪等性・Temporal Schedule の維持 | 確認する | 差分に冪等キー・予約台帳・Schedule 定義の変更が無いこと、`.env` 不変 |
| B9 | 新 flag がすべて OFF | PASS | 本番 `.env` に未設定、compose は `${BGM_ENABLED:-false}` |
| B10 | 24h 以内のバックアップと復旧演習（`restore_verified: true`） | 実施する | `scripts/backup.py run` → `restore-drill`（ADR-0051） |
| B11 | 隔離環境での integration test | 実施する | 本番と別の compose project・別 port・別 volume・資格情報なし（`--env-file /dev/null`）。本番 `avp2` に触れない |
| B12 | 稼働中 workflow 0・未照合予約 0・open な投稿予約なし | 確認する | preflight `running_workflows` / `provider_reservations` |
| B13 | Daily（21:00Z）と重ならない時刻 | 確認する | deploy は maintenance pause（`make deploy-workers`）で包む |
| B14 | 停止・復旧手順 | PASS | `--rollback`（303f33e、`SKIP_MIGRATE=1`）。DB 変更なし |
| B15 | デプロイ後の新規課金・投稿・Schedule 二重実行なしの確認手段 | 代替監視（§3.3） | OpenSearch に依存しない |
| B16 | release preflight（承認込み）が READY、事後検証が READY | 実施する | release-runbook §1・§3 |

注: D-1（UNKNOWN で投稿しない・承認の消費）は flag の無い安全側の挙動変更で、デプロイすると即時に有効になる。

各行の実測結果はリリース記録（`docs/releases/`）に残す。この表の「確認する／実施する」は、結果が揃うまで PASS とみなさない。

### 3.3 OpenSearch を停止したまま運用する判断

- **所有者判断（2026-10-29）: OpenSearch（`avp2-logging-opensearch-1`）は意図的に停止したまま運用を続ける。**
  起動・再作成・設定変更はしない。復旧は今回の作業対象外。`avp-logging-check.service` の failed も同じ理由で放置する。
- OpenSearch の停止は、**単独ではデプロイの BLOCKED 理由にしない**。release preflight の必須チェックにも
  OpenSearch は含まれない（`domain/release_preflight.py` の `PRE_DEPLOY_REQUIRED`）。
- 代替監視（デプロイ前の基準値とデプロイ後の比較に使う。すべて読み取り）:

  | 見るもの | 手段 | 何を確かめるか |
  |---|---|---|
  | アプリのログ | `docker logs --since <deploy 開始> avp2-<service>-1` | 起動エラー・例外・予期しない fal / YouTube 呼び出しの痕跡がない |
  | Temporal 履歴 | `temporal workflow list` / `schedule describe`（読み取り） | Running 0、Schedule の paused・spec がデプロイ前の記録と同じ、同じ Schedule の起動が二重でない |
  | PostgreSQL 監査台帳 | `BEGIN READ ONLY` の SELECT | `provider_reservations` の `reserved` 0・新規行なし、`youtube_upload` の新規行なし、`provider_credit_holds` の状態不変、`alembic_version` 0018 |

- 代替監視で確かめられない（OpenSearch を前提にした）長期の検索・集計は失われたまま。これはデプロイ判定ではなく
  運用上の既知の穴として残す。

## 4. 未解決リスクと残作業

1. OpenSearch は所有者判断で停止を維持する（§3.3）。復旧は対象外。長期のログ検索は代替監視で補えない既知の穴。
2. 隔離環境での integration test（判定 B11）。
3. 新しいバックアップと restore drill（判定 B10）。
4. QualityGate の検査入力の保存先と migration 計画（後続 ADR）、手動承認の DB 記録、API 認証。
5. `QualityCheck.detail` の自由文（LLM レビューを有効にする前に allowlist 化）。
6. 地図の結線（`check_scene_map_accuracy` を使う、hash の結び付け、CJK フォント）、BGM のラウドネス閾値の校正、
   ADR-0077 の承認、BGM のラウドネス失敗時の扱い（現状はレンダ失敗）。
7. tags 単独照合の実機検証（所有者が `scripts/verify-tags-only-match.py --live` を実行）。旧動画は説明欄にしか marker が無い。
8. 既存 `tests/architecture/test_geo_boundaries.py` の import 検出の抜け（範囲外のため未修正）。

## 5. 追加コストの見込み

- 今回の変更による API 課金: 0（fal・YouTube・LLM の新規呼び出しなし。flag OFF）。
- flag を ON にした場合も、地図・BGM・品質ゲートは決定論で fal を呼ばない。LLM レビューは未実装のため 0。
