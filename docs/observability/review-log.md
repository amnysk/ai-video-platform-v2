# ADR-0040 レビュー記録

## 設計レビュー（2026-09-30）
- 独立レビュー D-1〜D-20、producer A-1〜A-18、consumer B-1〜B-12 → 6cde6e9 / 01eb0ee で反映。
- 独立再確認: 新規 Critical/High なし。条件 D-21〜D-23 を 01eb0ee で反映し ADR Accepted。

## 実装レビュー（2026-10-05、対象 01eb0ee..b244207、担当D）
Critical なし。3271 passed（unit/contract/architecture）。

| ID | 重大度 | 担当 | 指摘 | 状態 |
|---|---|---|---|---|
| I-1 | High | A | 故障注入（INV-38）が INFO 発行・JsonFormatter・after_commit を通っていない（logger が INFO 無効のまま）。root DEBUG + configure_logging(stream=devnull) で注入し、発火回数 > 0 を assert、ledger.defer/_after_commit/CallObservation/reservation_fields も注入点に | 修正（9f8d35d、A）。D 再確認: 解消（2026-10-06、87fd105） |
| I-2 | Medium | A | emit() 外の引数計算が未保護（youtube uploader `_observe`/`_error_reasons` が except 節内で例外を置換し得る）。`record_upload_session` にログ用の DB 読み取り追加（§9 違反）→削除 | 修正（87cbe34、A）。D 再確認: 解消（2026-10-06、87fd105） |
| I-3 | Medium | A | INV-40 の旧履歴 replay が Production/Render/Upload/Storyboard/Pipeline に無い。01eb0ee で履歴 fixture を採り Replayer で検査 | 修正（caf691f、A）。D 再確認: 解消（01eb0ee の旧コードで 21/21 replay）。残り: ScriptWorkflow の通常・失敗経路の旧履歴が無い（Low、A）→ 修正（5a67eb7、A。Script 6本を ScriptWorkflow・EvidenceScriptWorkflow の両方で replay）。D 再確認: 解消（2026-10-06、87fd105） |
| I-4 | Medium | A | stack を安全化前に切り詰めている（formatter.py:171-188→303）。block ごとに sanitize→切詰 | 修正（d1bcbc5、A）。D 再確認: 解消（2026-10-06、87fd105） |
| I-5 | Medium | B | `AVP_ENVIRONMENT` 既定 dev のため prod index に dev が入る。既定を外し unknown、deploy 手順に prod 明記、check-pipeline で不一致検出 | 修正（4f72c42, 3a570c9）。D 再確認: 解消（2026-10-06、87fd105） |
| I-6 | Low | B | check-pipeline の lag を系統別に、curl のパスワードを argv に出さない | 修正（3a570c9）。D 再確認: 解消（2026-10-06、87fd105） |
| I-7 | Low | B/契約 | infra フィールド集合を contracts へ、Lua の unstructured 出力 ⊆ infra mapping を test で固定 | 修正（56bf259）。D 再確認: 解消（2026-10-06、87fd105） |
| I-8 | Low | A | workflow の `_event()` 重複、stage 文字列 ⊆ LogStage を AST テストで固定 | 修正（4b6a114、A）。D 再確認: 解消（2026-10-06、87fd105） |
| I-9 | Low | B | one-shot が pki/ 全体（秘密鍵含む）を mount | 修正（838ce5d、追加修正 dbca5de: securityadmin の DAC_READ_SEARCH）。D 再確認: 解消（2026-10-06、87fd105） |
| I-10 | Low | A/B | Dockerfile app stage の CMD を `python -m apps.api.serve` に | 修正（935ced0、A）。D 再確認: 解消（2026-10-06、87fd105） |

## 実装レビュー 再確認（2026-10-06、対象 3bebec3..87fd105、担当D）
Critical/High の残りなし。I-1〜I-10 は解消（I-3 は上記の残りあり）。3305 passed。新規 Low:

| ID | 重大度 | 担当 | 指摘 | 状態 |
|---|---|---|---|---|
| I-11 | Low | A | `log_guard` の本体を AST で制限する検査が無い（`Raise`・`Return`・`Await` 禁止、呼び出しは `emit`/`defer` と許可したログ用関数のみ）。`test_logging_boundaries.py` に追加 | 修正（99aafd0、A）。D 再確認: 解消（2026-10-07、統合 64d6e1c まで） |
| I-12 | Low | A | 故障注入の `LEDGER_SUITES` に `test_fal_storage.py`、uploader・render・research・pipeline の Activity テスト、`test_log_ledger.py` が無い。追加し発火回数 > 0 を assert | 修正（9f3f385、A。記録を見る fal_storage・log_ledger は別検査で「落ちるのは記録を見る行だけ」を確認）。D 再確認: 解消（2026-10-07、統合 64d6e1c まで） |
| I-13 | Low | B | check-pipeline の app lag 既定 60分が watchdog の毎時実行（35 * * * *）と同周期で誤報しやすい。90分以上にし根拠を platform.md へ | 修正（cb9cc83、B）。D 再確認: 解消（2026-10-06、コーディネーター経由） |
| I-14 | Low | B | 本番 `.env` に `AVP_ENVIRONMENT` が無い（キーの有無のみ確認）。platform.md の導入手順をチェックリスト化（`AVP_ENVIRONMENT=prod` 追記・`read_from_head` 二段構え・deploy 前の追いつき確認・systemd timer）。本番 `.env` 自体は触らない | 修正（7cfd1b2、B）。D 再確認: 解消（2026-10-06、コーディネーター経由） |

## 隔離試験（担当C フェーズ2、2026-10-06、claude/oslog-verify 6e1e720）で見つかった不具合
C の作業メモでは ID が F-B1 と重複していたため、ここで I 番号を振り直す。

| ID | 重大度 | 担当 | 指摘 | 状態 |
|---|---|---|---|---|
| I-15 | High | B | Fluent Bit tail の stall: Docker が partial に分けた1行が結合後 `buffer_max_size`（256k）を超えると、tail が CPU 100% のまま全ファイルの読み取りを止める。health ok・`long_line_skipped` 0 で、lag でしか見えない。業務影響は無いがログ収集が無言で全停止する | 修正（8b97fda、検知 7dc4d7e、B）。原因は buffer_max_size ではなく、結合後の行に上限が無いこと＋安全化 Lua の O(n²)（platform.md §3「長い行」）。D 再確認: 停止は解消、線形化で伏せ字の後退2点（I-22・I-23）→ a7dea61 で修正。D 再確認: 解消（2026-10-07、統合 64d6e1c まで） |
| I-16 | Low | B | 自己署名 CA に keyUsage が無く、Python 3.13 の `VERIFY_X509_STRICT` で拒否される（試験用 search-assert.py は strict だけ外して回避） | 修正（5cf2fba、B。既存の証明書は再生成が必要）。D 再確認: 解消（2026-10-06、コーディネーター経由） |
| I-17 | Medium | B | check-pipeline が位置 DB の offset と json-file サイズの差（追いつき）を見ない。deploy 前確認が試験用 `catchup.py` 頼み | 修正（7dc4d7e、B）。D 再確認: 解消（2026-10-06、コーディネーター経由） |
| I-20 | Low | A | （担当C V-3）並行二重起動の upload 試験で動画1本に `upload.succeeded` が2件（どちらも `reconciled_by=upload_response`）。業務は1本（`videos_created` 1・予約1つ）。2件目は事実と違う記録: 負けた試行が台帳の spent を読んで同じ video id を no-op で再記録し、succeeded と `reservation.spent` を重ねて出していた | 修正（9780b54、A。succeeded はこの試行が spent を書いた時だけ、他は `upload.reused_existing`（`found_at=record`）、`reservation.spent` は no-op で出さない）。D 再確認: 解消（2026-10-07、統合 64d6e1c まで） |
| I-21 | Low | A | （D、統合 94d52f4）I-20 の並行テスト `test_concurrent_upload_attempts_log_one_success_and_one_reuse` が30回に1回失敗。負けた試行が `InvalidTransitionError('job transition rejected: running + started')` で止まり reuse 経路に届かない回がある | 修正（7ffbbcd、A。原因はテストの前提（負けた側が必ず reuse まで進む）で、ログ発行・業務は正しい。2つの順序をそれぞれ固定した2本に置き換え、100 回連続 0 失敗）。D 再確認: 解消（2026-10-07、統合 64d6e1c まで） |

## 隔離試験（担当C、verification-results.md の V-1・V-2）で見つかった不具合

| ID | 重大度 | 担当 | 指摘 | 状態 |
|---|---|---|---|---|
| I-18 | Medium | B | 数値フィールドの文字列 "inf"/"nan" を Lua の型修復が数値として通し、Bulk が chunk ごと失敗して同じ chunk の正常行も届かない（41行中0件）。再送は破棄・エラーの metrics に出ず retry だけ増える（C の V-1） | 修正（2d4c420、B）。D 再確認: 解消（2026-10-06、コーディネーター経由） |
| I-19 | Low | B | `{` で始まるが JSON として壊れた行が app index に必須フィールド無しで入る。設計（ADR-0040 §1・platform.md §1）は infra（C の V-2） | 修正（2760ae6、B）。D 再確認: 解消（2026-10-06、コーディネーター経由） |

## D の再確認（2026-10-06、I-13〜I-19 の B 修正、コーディネーター経由）
B の I-13・I-14・I-16〜I-19 は解消。I-15 の線形化に伏せ字の後退が2点。

| ID | 重大度 | 担当 | 指摘 | 状態 |
|---|---|---|---|---|
| I-22 | Low | B | JWT の直前に形の合わない `eyJ.` があると後ろの本物の JWT が伏せられない（87fd105 の規則は伏せていた） | 修正（a7dea61、B。部分ごとに形を確かめ、一番左の形の合う `eyJ` から伏せる。差分 fuzz 2,000,000 入力で新規則だけが漏らす件数 0）。D 再確認: 解消（2026-10-07、統合 64d6e1c まで） |
| I-23 | Low | B | 閉じていない PRIVATE KEY の BEGIN の後に別の BEGIN が来ると2つ目の鍵本文が残る（終端が BEGIN の見出しにも一致） | 修正（a7dea61、B。終端を END の見出しだけに。旧規則に無い追加の規則は旧規則の後に流す）。D 再確認: 解消（2026-10-07、統合 64d6e1c まで） |
| I-24 | Low | B | 結合後の行は Lua に渡るまで全体がメモリに載り、数十〜数百MB の1行で Fluent Bit（256MiB）が OOM→再起動で同じ行を読み直しうる | 記録（092d219、platform.md §3 に限界・兆候・対処の手順。手順は未実測） |
| I-25 | Low | B | catchup の複製中の書き込みによる誤報、inode 再利用での見逃し | 記録（092d219、platform.md §5 に既知の限界） |

## 担当C 最終版試験（verification-results.md §5、V-8・V-9）で見つかった不具合

| ID | 重大度 | 担当 | 指摘 | 状態 |
|---|---|---|---|---|
| I-26 | Medium | B | Fluent Bit の `docker restart` でその瞬間の 1〜2 行が届かない（3/3 回）。行は json-file に残り、位置 DB は先へ進んでいる。metrics に出ない（C の V-8） | 既知の限界として記録（6df86df、文言の整理 ac48c45、B）。rewrite_tag があるときだけ起きる（Fluent Bit 単体で再現: restart 10 回で 3600 行中 10 行、rewrite_tag 無しなら 0）。設定（grace・emitter_storage・tail の storage.type・flush）では直らない。直すには tail を系統ごとに分けて rewrite_tag を外す設計変更（ADR-0040 §1）が要る。緩和策の読み直しは I-28 で動くようにした |
| I-27 | Low | B | `dropped_records_total` が実欠損と合わない（届いた + dropped が出力を 100047 上回る）（C の V-9） | 修正（125128d、B。check-pipeline は増えたことの検知だけに使い件数を実数として報告しない。platform.md §3/§8。計上の詳細は未特定）。D 再確認: 解消（2026-10-07、統合 64d6e1c まで） |
| I-30 | Low | C | 試験用 `deploy/logging/test/catchup.py` が本番用 `deploy/logging/scripts/catchup.py`（I-17）と同じ処理の二重実装（AGENTS.md §8）。verification-results.md が証拠の置き場として scratchpad・`/run/user/…`（セッション限りで消える）を挙げている | 修正（d2b6dd9、C。`lib.sh` の `catchup` は本番の catchup.py を対象 project のコンテナ ID と許容差 0 で呼び、試験用を削除。同じ位置 DB の複製で両者の未読 1407827 bytes・同じファイルが一致、隔離環境で harness と `check-pipeline.sh --catchup-only` がともに 0。results は消える場所を注記し、数値は本文に転記済み・一時 probe を §7 に収録（C、本 commit の前）。D 再確認: 解消（2026-10-07、統合 64d6e1c まで） |

## D の再確認（I-26・I-27 以降、コーディネーター経由）

| ID | 重大度 | 担当 | 指摘 | 状態 |
|---|---|---|---|---|
| I-28 | Low | B | I-26 の緩和策「位置 DB を消して読み直す」が既定起動で動かない（guard が位置 DB 無し＋`read_from_head=true` を止める。false では黙って読み直さない）。app の重複無しに rollover 済み index の但し書きが無い | 修正（283725a、B。guard に明示の読み直しモード `AVP_LOG_REREAD=yes`、runbook §7・platform.md §3 の手順。隔離環境で1回実行: 500 行を読み直し、以後の既定起動では読み直さない）。D 再確認: 解消（2026-10-07、統合 64d6e1c まで） |
| I-29 | Low | B | `test_dependency_single_source.py` が `deploy/logging/test/Dockerfile.test-runner` を見ない | 修正（a89a408、B。依存の再掲とパッケージ名の直接追加を検査。足すと落ちることを確認）。D 再確認: 解消（2026-10-07、統合 64d6e1c まで） |

## 最終状態（2026-10-07、統合 HEAD 64d6e1c）
- D の独立再確認: I-1〜I-30 すべて解消（I-26 は既知の限界として記録・ADR-0040 §5 に追記、I-24・I-25 は限界の記録）。Critical/High の残りなし。
- チェック: ruff・ruff format・pyright 0件、`pytest tests/unit tests/contract tests/architecture` 3352 passed（親と D がそれぞれ実行）。Lua integration 6 passed（c5af8ff、単独・network none）。
- 隔離試験（担当C）: 最終版で S-E2E〜S-RB を実施、取り込み→検索の証拠は `verification-results.md` §5（D が読み取りで数値を独立再現、§6）。

## 残件・未実施
- **I-26 の根本対処（所有者判断）**: tail を app/infra に分け rewrite_tag を廃止すれば restart 時の欠損が 0（B の実測 0/3600）。収集経路の変更で ADR-0040 の改訂を伴う。受け入れる欠損として扱うことを PR で所有者が了承する必要がある。
- S-RB 第3段（compose の `logging:` を戻す）: 隔離環境で差し替える手段が無く未実施。
- check-pipeline の tail 停滞検知: 修正後は停滞が再現しないため実機で未発火（修正前の Lua での発火は B が確認）。
- I-24 の巨大行回避手順・I-28 の読み直し手順の本番 project 名での実行は未実施（隔離 project 名で確認済み）。
- S-403 の `provider.call.failed`、S-REUSE の `artifact.reuse_rejected`: fake provider の integration では発生しない（V-5、計画側の記述ずれ）。
- 担当B の作業中に unit+contract+architecture の全体実行で 1 failed が2回（テスト名未取得）。親の統合版での連続4回・D の実行ではすべて pass で再現せず。並行試験の負荷による可能性があるが未特定。
