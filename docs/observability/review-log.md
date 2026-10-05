# ADR-0040 レビュー記録

## 設計レビュー（2026-09-30）
- 独立レビュー D-1〜D-20、producer A-1〜A-18、consumer B-1〜B-12 → 6cde6e9 / 01eb0ee で反映。
- 独立再確認: 新規 Critical/High なし。条件 D-21〜D-23 を 01eb0ee で反映し ADR Accepted。

## 実装レビュー（2026-10-05、対象 01eb0ee..b244207、担当D）
Critical なし。3271 passed（unit/contract/architecture）。

| ID | 重大度 | 担当 | 指摘 | 状態 |
|---|---|---|---|---|
| I-1 | High | A | 故障注入（INV-38）が INFO 発行・JsonFormatter・after_commit を通っていない（logger が INFO 無効のまま）。root DEBUG + configure_logging(stream=devnull) で注入し、発火回数 > 0 を assert、ledger.defer/_after_commit/CallObservation/reservation_fields も注入点に | 修正（9f8d35d、A）。D の再確認待ち |
| I-2 | Medium | A | emit() 外の引数計算が未保護（youtube uploader `_observe`/`_error_reasons` が except 節内で例外を置換し得る）。`record_upload_session` にログ用の DB 読み取り追加（§9 違反）→削除 | 修正（87cbe34、A）。D の再確認待ち |
| I-3 | Medium | A | INV-40 の旧履歴 replay が Production/Render/Upload/Storyboard/Pipeline に無い。01eb0ee で履歴 fixture を採り Replayer で検査 | 修正（caf691f、A）。D の再確認待ち |
| I-4 | Medium | A | stack を安全化前に切り詰めている（formatter.py:171-188→303）。block ごとに sanitize→切詰 | 修正（d1bcbc5、A）。D の再確認待ち |
| I-5 | Medium | B | `AVP_ENVIRONMENT` 既定 dev のため prod index に dev が入る。既定を外し unknown、deploy 手順に prod 明記、check-pipeline で不一致検出 | 未修正 |
| I-6 | Low | B | check-pipeline の lag を系統別に、curl のパスワードを argv に出さない | 未修正 |
| I-7 | Low | B/契約 | infra フィールド集合を contracts へ、Lua の unstructured 出力 ⊆ infra mapping を test で固定 | 未修正 |
| I-8 | Low | A | workflow の `_event()` 重複、stage 文字列 ⊆ LogStage を AST テストで固定 | 修正（4b6a114、A）。D の再確認待ち |
| I-9 | Low | B | one-shot が pki/ 全体（秘密鍵含む）を mount | 未修正 |
| I-10 | Low | A/B | Dockerfile app stage の CMD を `python -m apps.api.serve` に | 修正（935ced0、A）。D の再確認待ち |

## 未実施
- 担当C フェーズ2（隔離 compose での取り込み・検索までの試験）: claude/oslog-verify（46e5476）に A+B を merge 済み、未着手。
- 上記修正後の D による独立再確認。
