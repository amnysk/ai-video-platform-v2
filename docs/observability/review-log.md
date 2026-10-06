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
| I-3 | Medium | A | INV-40 の旧履歴 replay が Production/Render/Upload/Storyboard/Pipeline に無い。01eb0ee で履歴 fixture を採り Replayer で検査 | 修正（caf691f、A）。D 再確認: 解消（01eb0ee の旧コードで 21/21 replay）。残り: ScriptWorkflow の通常・失敗経路の旧履歴が無い（Low、A）→ 修正（5a67eb7、A。Script 6本を ScriptWorkflow・EvidenceScriptWorkflow の両方で replay）。D の再確認待ち |
| I-4 | Medium | A | stack を安全化前に切り詰めている（formatter.py:171-188→303）。block ごとに sanitize→切詰 | 修正（d1bcbc5、A）。D の再確認待ち |
| I-5 | Medium | B | `AVP_ENVIRONMENT` 既定 dev のため prod index に dev が入る。既定を外し unknown、deploy 手順に prod 明記、check-pipeline で不一致検出 | 修正（4f72c42, 3a570c9）。D の再確認待ち |
| I-6 | Low | B | check-pipeline の lag を系統別に、curl のパスワードを argv に出さない | 修正（3a570c9）。D の再確認待ち |
| I-7 | Low | B/契約 | infra フィールド集合を contracts へ、Lua の unstructured 出力 ⊆ infra mapping を test で固定 | 修正（56bf259）。D の再確認待ち |
| I-8 | Low | A | workflow の `_event()` 重複、stage 文字列 ⊆ LogStage を AST テストで固定 | 修正（4b6a114、A）。D の再確認待ち |
| I-9 | Low | B | one-shot が pki/ 全体（秘密鍵含む）を mount | 修正（838ce5d、追加修正 dbca5de: securityadmin の DAC_READ_SEARCH）。D の再確認待ち |
| I-10 | Low | A/B | Dockerfile app stage の CMD を `python -m apps.api.serve` に | 修正（935ced0、A）。D の再確認待ち |

## 実装レビュー 再確認（2026-10-06、対象 3bebec3..87fd105、担当D）
Critical/High の残りなし。I-1〜I-10 は解消（I-3 は上記の残りあり）。3305 passed。新規 Low:

| ID | 重大度 | 担当 | 指摘 | 状態 |
|---|---|---|---|---|
| I-11 | Low | A | `log_guard` の本体を AST で制限する検査が無い（`Raise`・`Return`・`Await` 禁止、呼び出しは `emit`/`defer` と許可したログ用関数のみ）。`test_logging_boundaries.py` に追加 | 修正（99aafd0、A）。D の再確認待ち |
| I-12 | Low | A | 故障注入の `LEDGER_SUITES` に `test_fal_storage.py`、uploader・render・research・pipeline の Activity テスト、`test_log_ledger.py` が無い。追加し発火回数 > 0 を assert | 修正（9f3f385、A。記録を見る fal_storage・log_ledger は別検査で「落ちるのは記録を見る行だけ」を確認）。D の再確認待ち |
| I-13 | Low | B | check-pipeline の app lag 既定 60分が watchdog の毎時実行（35 * * * *）と同周期で誤報しやすい。90分以上にし根拠を platform.md へ | 未修正 |
| I-14 | Low | B | 本番 `.env` に `AVP_ENVIRONMENT` が無い（キーの有無のみ確認）。platform.md の導入手順をチェックリスト化（`AVP_ENVIRONMENT=prod` 追記・`read_from_head` 二段構え・deploy 前の追いつき確認・systemd timer）。本番 `.env` 自体は触らない | 未修正 |

## 隔離試験（担当C フェーズ2、2026-10-06、claude/oslog-verify 6e1e720）で見つかった不具合
C の作業メモでは ID が F-B1 と重複していたため、ここで I 番号を振り直す。

| ID | 重大度 | 担当 | 指摘 | 状態 |
|---|---|---|---|---|
| I-15 | High | B | Fluent Bit tail の stall: Docker が partial に分けた1行が結合後 `buffer_max_size`（256k）を超えると、tail が CPU 100% のまま全ファイルの読み取りを止める。health ok・`long_line_skipped` 0 で、lag でしか見えない。業務影響は無いがログ収集が無言で全停止する | 未修正 |
| I-16 | Low | B | 自己署名 CA に keyUsage が無く、Python 3.13 の `VERIFY_X509_STRICT` で拒否される（試験用 search-assert.py は strict だけ外して回避） | 未修正 |
| I-17 | Medium | B | check-pipeline が位置 DB の offset と json-file サイズの差（追いつき）を見ない。deploy 前確認が試験用 `catchup.py` 頼み | 未修正 |
| I-20 | Low | A | （担当C V-3）並行二重起動の upload 試験で動画1本に `upload.succeeded` が2件（どちらも `reconciled_by=upload_response`）。業務は1本（`videos_created` 1・予約1つ）。2件目は事実と違う記録: 負けた試行が台帳の spent を読んで同じ video id を no-op で再記録し、succeeded と `reservation.spent` を重ねて出していた | 修正（9780b54、A。succeeded はこの試行が spent を書いた時だけ、他は `upload.reused_existing`（`found_at=record`）、`reservation.spent` は no-op で出さない）。D の再確認待ち |

## 未実施
- 担当C フェーズ2: 途中まで実施（integration を1ファイルずつ隔離 runner で実行、故障注入・ISM 監視・資源測定の一部）。中断のため `verification-results.md` 未作成。最終統合版で再実行する。
- 上記修正後の D による独立再確認。
