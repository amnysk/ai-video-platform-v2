# 品質ゲートの過剰停止の防止: テストを置いた理由（ADR-0087 §8 / §11、INV-156 / INV-157）

背景: 本番 58 Episode の blocked 17 件に品質ゲート起因は 0 件（`AVP_QUALITY_GATE_ENFORCE` は一度も ON でない）。
ON にした後に起きうる過剰停止は構造上 1 つ: producer の一時障害（MinIO の瞬断・DB のタイムアウト）が「記録が無い」になり、
必須検査の欠落 → REVIEW → Episode が BLOCKED。本物の品質の保留と区別がつかず、再試行も無かった。
守る境界は 2 つ: **一時障害だけを再試行する**ことと、**何があっても拒否を許可にしない**こと。

## 分類（`tests/unit/test_quality_inspection_classification.py`）

- `test_classification_table`（表の 1 行 = 1 ケース、33 行）: 再試行してよい失敗（接続・タイムアウト・I/O・DB の接続障害・
  MinIO の 5xx 系）と、してはいけない失敗（hash 不一致・別動画の証跡・契約違反・権限・存在しない・DB の一意制約違反）の境界。
  表は `infrastructure/quality/inspection.py` の 1 箇所だけにあり、ここが仕様の写し。恒久を再試行すると hash 不一致のような
  本物の破損を 3 回叩いて時間を使い、一時を恒久にすると過剰停止が残る。`BgmEvidenceError` は原因（`__cause__`）で分かれる
  （「読めない」= I/O なら一時、「別の動画のもの」= 恒久）ことも行で固定する。
- `test_cause_chain_is_bounded_and_total`: 原因の鎖が長い・自己参照でも分類が止まる（gate の中で無限ループしない）。

## 純粋部分（`tests/unit/test_quality_inspection_domain.py`）

- 再試行の方針（3 回・指数・合計 30 秒以内）と、30 秒を超える設定を作れないこと: ADR の上限が設定変更で黙って伸びない。
- 欠落の理由の区別: 一時障害で作れなかったセットだけが `inspection_unavailable`、隣の本物の欠落は `required_check_missing`
  のまま。区別された検査も REVIEW を下回らない（PASS/WARN にならない）。
- `refine_hold_reason`: 保留の原因が検査系の故障**だけ**のときだけ理由を差し替える。品質の保留・BLOCK が混じれば本来の理由
  を残す（品質の問題を「ただの故障」に見せない）。許可・他の拒否理由（期限切れ等）は触らない。

## gate の再試行と回復（`tests/unit/test_quality_gate_inspection_recovery.py`）

実の render で作った final_video と sqlite を使い、ストアや DB を壊す。sleep と時計は注入する（実時間を待たない）。

- `..._is_retried_and_the_episode_recovers`: 2 回までの一時障害は待ち 1 秒・2 秒で回復して PASS になる（過剰停止の本体）。
- `..._persistent_transient_fault_is_review_with_inspection_unavailable`: 回復しないときも PASS に倒れず、BLOCK でもなく、
  理由が `inspection_unavailable`。保存された報告の各検査にも区別が残り、偽の記録（`quality_check_records`）は書かない。
- `..._permanent_defect_is_not_retried_and_stays_a_quality_review`: 恒久は待たない。理由は従来どおり。
- `..._total_budget_stops_retrying_before_thirty_seconds`: 評価全体の経過が 30 秒を超えるなら待たずに諦める（Activity の
  タイムアウトを食い潰さない）。
- `..._no_failure_pattern_ever_yields_an_allowance_unless_recovered_cleanly`（例外 8 種 × 失敗回数 4）: 許可になるのは
  必須検査が実際に記録できたときだけ。それ以外は必ず 3 つの拒否理由のどれか。
- `..._a_genuine_block_is_not_hidden_behind_inspection_unavailable`: 片方が故障でも、もう片方の本物の BLOCK が見える。
- 報告の保存・記録の読み出しの DB 障害 3 件: 一時は再試行（二重に保存しない）、回復しなければ `inspection_unavailable`
  で拒否（監査が残らない許可は出さない / INV-128）、恒久は従来の理由のまま。

## 保留率の警報（`test_quality_hold_rate.py` / `test_quality_hold_alarm_wiring.py`、`test_quality_gate_persisted_wiring.py` 末尾）

- 閾値（直近 10 件中 5 件）・窓・古い順の扱い・不正な引数・WARN は保留でないこと・原因の分類（品質 / 検査系）・保存済みの報告
  から判定を復元する規則が gate の区別と一致すること。
- 記録: 内訳つきで 1 行、同じ日は件数だけ増える（冪等）、翌日は別の行、率が戻れば今日の行を解決、DB が壊れていても例外を
  投げない（警報の故障で gate を止めない）。
- `test_upload_gate_reports_inspection_unavailable_and_records_the_hold_alarm`: 自動経路の `upload_gate` から、構造化ログの
  `hold_cause` と警報の異常行まで通しで確認する。
- `tests/contract/test_migration_0022_check_widening.py`: 新しい `AnomalyKind` を入れるための CHECK 拡張。旧コードの値が
  全て通り続ける・新行があれば downgrade を拒否・往復できる・release preflight が拡張として認める（0017 と同じ形）。
  0017 の「語彙は全 `AnomalyKind` と等しい」は、後続の拡張で必ず偽になるので部分集合に緩め、等号は最新の 0022 側が守る。

## WARN の方針（`tests/unit/test_quality_warn_policy.py`）

監査の結論: 現在 gate で WARN を作るのは地図の許容距離の超過（`domain/geo`）だけで、**この変更で WARN に変えた検査は無い**。
説明欄の任意部分の切り詰めは lint の対象外（PASS のまま）、必須部分が入らない場合は INV-112 で止める（投稿側も同じ入力で
失敗するので gate だけ WARN にしても整合しない）。BGM の発話差の未測定は検査自身が軽微と言えない（REVIEW）。
- 総当たりの表（検査 0〜3 個 × 4 判定 × 決定論/LLM）: 集計は決定論の最悪より緩くならず、LLM は BLOCK を作れず、
  REVIEW/BLOCK を含めば PASS/WARN にならない。
- 判定語彙ごとに「許可は PASS/WARN だけ」、理由コードの全値 × REVIEW/BLOCK で `refine_hold_reason` が許可を作らない。
- `test_warn_is_produced_only_by_the_audited_deterministic_checks`: `Verdict.WARN` を書くファイルの許可リスト。
  新しい「軽微」をレビューなしに足すと落ちる。

## 集計と影の評価（`test_quality_rates_script.py` / `test_quality_shadow_script.py`）

- `quality_rates.py`: 品質の保留と検査系の保留が率で混ざらない・Episode ごとの最新・空の DB・書き込まない（ソースに
  書き込み文が無く、READ ONLY を宣言する）・DB の URL が必須。
- `quality_shadow.py`: 実の render の Episode を書き出して旧・新 gate を当てる。書き出しを変更しない、注入した一時障害が
  予算内なら `auto_recovered`、予算超過なら `inspection_unavailable`（PASS にならない）、改ざんは本物の品質の保留、
  使用宣言の扱い、壊れた Episode が他を止めない、本番に接続する記述が無い。
