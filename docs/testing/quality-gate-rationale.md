# テスト設計の根拠: 品質ゲートの契約と判定（ADR-0073、INV-101〜103）

実際の事故から作ったテストではない。設計 v3 のテスト T04 / T22 / T32 を、配線（`upload_gate`）から
独立した純粋な契約・判定として先に固定したもの。DB・Temporal・provider・ネットワークには触れない。
INV-104（`upload_gate` が品質レポート無しで拒否する）は配線が無いので**未検査**。

## `tests/contract/test_quality_contract.py`

| テスト | なぜ必要か |
|---|---|
| `test_a_well_formed_report_round_trips` | 生成側と取り込み側が同じ契約を通る。片側だけ変わると保存物が読めず、全件が拒否に倒れる |
| `test_malformed_reports_are_rejected_with_a_reason` | 取り込みの不備（未知の verdict・schema 違い・sha/時刻/列挙値の不正・型違い）が `None` や PASS に化けず、理由コードつきの例外になる（INV-101） |
| `test_non_report_input_is_unparsable` | 空・壊れた JSON・配列・`null`・不正バイト・数値は全て取り込めない |
| `test_a_non_pass_check_must_carry_a_real_reason_code` | 不合格なのに理由が `ok` の検査を作れない（理由の無い BLOCK は人が追えない） |
| `test_reason_codes_are_unique_strings` | 理由コードが1つの enum で一意（定義は1箇所 / AGENTS.md §8） |

## `tests/unit/test_quality_aggregate.py`（INV-102 / T32）

| テスト | なぜ必要か |
|---|---|
| `test_all_pass_…` / `test_warn_is_reported_…` / `test_review_beats_warn` | 重さの順序（WARN は止めない、REVIEW > WARN） |
| `test_deterministic_block_beats_everything` | 決定論の BLOCK が最優先 |
| `test_llm_pass_cannot_relax_…`（BLOCK / REVIEW） | T32 そのもの。批評 LLM の誤った PASS で決定論の判定が緩まない。順序を入れ替えても同じ |
| `test_llm_can_only_make_it_stricter_…` | LLM は厳しくする側だけ。LLM の BLOCK は REVIEW 止まり（自動で落とさず人が見る） |
| `test_llm_only_pass_is_still_not_proof_of_pass` / `test_empty_checks_…` | 決定論の根拠が無い・検査が空は PASS にならない |
| `test_adding_an_llm_check_never_relaxes_the_result` | 決定論 2 検査 × LLM 検査の全組み合わせで単調（例示テストの抜けを塞ぐ） |

## `tests/unit/test_quality_upload_decision.py`（INV-101 / T22）

拒否経路ごとに 1 テスト: レポート無し・作成失敗・取り込めない入力・schema 違い・未知 verdict・検査が空・
final_video 違い（再 render で古い PASS が効かない）・input_hash 違い・現在の sha が空・
保存 verdict が検査と矛盾（改ざん・書き間違い）・未来日付・BLOCK/REVIEW。許可は PASS と WARN（報告つき）だけ。
LLM の PASS が決定論 BLOCK を救えないことを判定の入口でも確認する。verdict と Episode 状態の対応は
既存の状態だけを返す（状態を足していない）ことも固定した。

## `tests/unit/test_agent_tool_policy.py`（INV-103 / T04）

許可リストが正確に6つ・禁止集合と交わらない・許可の全 tool が通る・禁止 tool は `FORBIDDEN` で拒否・
資料に埋め込まれた `youtube.upload` は構造化された拒否（記録できる形）・大文字小文字/空白違いの似た名前や
不正な型は拒否・長い未知 id は記録で切り詰める。

## `tests/architecture/test_quality_gate_boundaries.py`

| テスト | なぜ必要か |
|---|---|
| `test_quality_and_agent_tool_modules_do_no_io_…` | 判定層が I/O・provider・予約台帳・workers に依存すると、純粋に検査できず、課金・投稿への経路になる |
| `test_the_io_detector_catches_violating_fixtures` | 検査の検査。検出器が壊れて常に空を返しても緑になるのを防ぐ |
| `test_forbidden_tool_ids_are_not_in_the_allowlist` | 許可リストへ禁止 tool が紛れ込む事故（実行時の交わり＋ソース上の `PolicyToolId` 本体） |
| `test_the_allowlist_detector_catches_a_violating_fixture` | 上の検出器が違反 fixture を実際に捕まえる |
| `test_the_forbidden_set_covers_…` | 禁止集合の縮小（例: 予約解放の削除）を検知する |

## 独立レビュー後の追加（回帰テスト。いずれも再現してから直した）

| テスト | なぜ必要か（再現した事故） |
|---|---|
| `test_error_messages_never_echo_untrusted_values` / `test_raw_text_is_never_echoed_either` | 取り込み失敗の例外文に `!r` で入力値（5000 文字の秘密らしい値）が入っていた。INV-20/39 に反する。型名と長さだけにし、文の長さも有界にした |
| `test_deeply_nested_json_is_unparsable_not_a_recursion_error`（契約・判定の両方） | `'[' * 100000` が `RecursionError` を投げ、判定が例外で落ちた。fail-closed は「拒否を返す」であって「落ちる」ではない |
| `test_schema_version_must_be_exactly_the_int` | `1.0 == 1` で `schema_version: 1.0` が通っていた |
| `test_naive_generated_at_is_refused_not_raised` / `test_naive_now_is_refused_not_raised` | naive と aware の比較が `TypeError` で判定を落とした |
| `test_clock_skew_within_tolerance_…` | 時計のずれの許容を1つの定数にし、境界（許容内は許可・1秒超は拒否）を固定する |
| `test_checks_must_be_a_tuple_of_quality_checks` / `test_a_directly_constructed_report_…` / `test_a_check_built_with_wrong_types_is_refused` | `checks` が list の report が `allowed=True` になっていた。直接構築した report にも取り込みと同じ型検査をかける |
| `test_no_input_can_make_evaluate_raise` / `test_an_unexpected_internal_error_fails_closed` | 総関数であること（未知の例外は `REPORT_EVALUATION_ERROR` で拒否） |
| `test_an_unknown_id_is_never_echoed_only_its_length_and_hash` / `test_known_ids_are_recorded_verbatim` | 拒否記録が資料由来の不明 id の先頭 64 文字を写していた（秘密・注入文が decision の記録に残る）。長さとハッシュだけにし、既知 id は監査のためそのまま残す |
| `test_case_whitespace_and_nfkc_variants_of_forbidden_ids_are_forbidden_attempts` | `YouTube.Upload` や全角表記が「ただの不明 tool」として埋もれていた。拒否は同じまま、禁止 tool を狙った試行として監査で区別できる |
| `test_variants_of_allowed_ids_are_still_just_not_allowlisted` | 許可 id の似た名前を曖昧に許可しない（許可は完全一致だけ） |

## `tests/unit/test_quality_gate_wiring.py`（INV-104 / ADR-0076。配線。SQLite + 実 repository）

| テスト | 不変条件 / レベル | なぜ必要か（捕まえる失敗） |
|---|---|---|
| `test_on_without_any_deterministic_check_is_refused` | INV-104 / 単体 | 検査が 0 件の Episode が「無検査 = PASS」で自動投稿される |
| `test_on_with_only_an_llm_pass_is_refused` | INV-102 / 単体 | LLM の自己採点だけで通る |
| `test_on_block_is_refused` / `test_on_review_is_refused_and_episode_stays_render_ready` | INV-104 / 単体 | BLOCK/REVIEW が許可に化ける。拒否時に Episode 状態を動かしてしまう（遷移表に無い遷移） |
| `test_on_pass_and_warn_are_allowed_...` | INV-104 / 単体 | 品質を足したことで正常系まで止まる |
| `test_on_pass_still_refused_when_reservation_is_dispatched` | INV-104 + 既存の予約検査 / 単体 | PASS が既存の拒否（二重投稿防止）を上書きする |
| `test_on_existing_refusals_come_before_quality` | INV-104 / 単体 | 既存の拒否理由が品質の理由に置き換わる |
| `test_on_a_provider_exception_is_refused_never_pass` | INV-101 / 単体 | 検査の例外で落ちる、または例外文（入力由来）が理由に載る |
| `test_on_an_evaluate_exception_is_refused` / `test_on_a_clock_exception_is_refused` | INV-101 / 単体 | 評価・時計の例外が PASS に倒れる |
| `test_on_logs_the_decision_with_contract_fields` | ADR-0076 §3 / 単体 | 判定の証跡（理由・Episode・sha256・規則版）が構造化ログに残らない |
| `test_builder_with_no_input_aggregates_to_review_with_no_model` | INV-101 / 単体 | 入力が無いのに PASS のレポートができる。model が入る |
| `test_builder_collects_checks_from_every_provider_in_order` | 拡張点 / 単体 | 他ブランチの検査を gate を触らずに足せなくなる |
| `test_technical_qa_and_description_providers` | INV-106 / 単体 | 「該当なし」が PASS になる。lint の文面に入力が写る |
| `test_llm_pass_cannot_lift_a_deterministic_block` | INV-102 / 単体 | 組み立て経由で LLM の PASS が BLOCK を緩める |
| `test_a_report_for_another_artifact_is_refused` / `test_a_stale_rule_version_is_refused_...` | INV-107 / 単体 | 別 artifact・古い規則版のレポートが根拠に使われる（gate が同じ鮮度検査を通る） |
| `test_manual_assessment_allows_review_but_never_block_or_failure` / `test_auto_assessment_never_approves_review` | INV-104 / 単体 | 手動の「REVIEW 許可」が BLOCK・作成失敗・自動経路へ漏れる |
| `test_off_gate_ignores_quality_even_with_block_and_broken_providers` / `test_off_gate_matches_on_gate_for_every_pre_existing_refusal` | INV-104（OFF 同値）/ 単体 | flag OFF で挙動が変わり、本番の既存の投稿が止まる（既存 gate テストを変えずに守る） |
| `test_setting_defaults_off_and_reads_the_single_key` | AGENTS §8 / 単体 | 既定が ON になる。キー名が複数箇所に散る |
| `test_manual_on_block_is_409` / `..._provider_failure_is_409` / `..._without_final_video_is_409` | INV-104 / API（SQLite） | 手動経路が品質を迂回する。拒否時に workflow を起動してしまう |
| `test_manual_on_review_is_allowed_and_the_approval_is_logged` / `test_manual_on_pass_is_allowed` | ADR-0076 §4 / API | 人の承認が記録されない。正常系が止まる |
