# Research への opt-in 接続（B6）のテストの理由

ADR-0039 §B6（Topic Planner）と ADR-0038 §B6（台本の Evidence 照合）、INV-37。B6 は Research Tier B の中で
**本番の日次の経路に触れる唯一の段**なので、テストの中心は「既定 OFF のとき接続前（f209e7c）とバイト単位で
同じ」を f209e7c から取った値で固定すること。ON の挙動は Fake だけで検査する（実ネットワーク・有料 API を呼ばない）。

## OFF が接続前と同じ

- `tests/unit/test_planner_trend_opt_in.py::test_off_prompt_and_version_are_byte_identical_to_f209e7c`:
  `generate_candidates` の prompt の sha256 と `prompt_version` を、**f209e7c の木**（`git archive f209e7c` を
  展開したもの）で同じ入力から取った golden（`tests/support/research_opt_in.py` の `OFF_PROMPT_SHA256`）と比べる。
  今の木から取った値と比べるだけだと、変更そのものを golden に焼き込んでしまう。
- 同じファイルの「Trend 無し」の検査（要約が `None`・読み口の例外・遅延・完了済みの Trend が無い・古すぎる・未来の
  観測・保存物の検証失敗）: ON にしても使える Trend が無ければ同じ golden に戻ることを確かめる。フラグだけでなく
  「ON だが Trend 無し」も接続前の挙動であることが INV-37 の「`completed` でなければ調査なし」の実体。
- `tests/unit/test_script_evidence_workflow_opt_in.py::test_the_f209e7c_off_history_replays_on_both_workers`:
  f209e7c と同じ `ScriptWorkflow`（B6 の変更前。`workers/planning` は f209e7c から変わっていなかった）で取った履歴
  （1 ラウンド目の retryable な失敗を含む）を、OFF の `ScriptWorkflow` と ON の `EvidenceScriptWorkflow` の両方で
  replay する。分岐を常に通す変異を入れると `NondeterminismError` で落ちることを手で確かめた（検査が空振りしない）。
  旧 `script_workflow_pre_0032_history.json` は visual style の型を含むので使わない。
- `tests/unit/test_script_evidence_workflow_opt_in.py::test_a_new_off_run_has_the_same_history_shape_as_f209e7c`:
  OFF で新しく走らせた履歴の event と Activity の列が fixture と同じで、patch の marker が無いこと。replay は
  「既存の履歴を再現できる」しか言わないので、「新しい履歴も同じ形」は別に検査する。
- `tests/unit/test_research_opt_in_off_path.py`: 設定の既定が OFF、OFF の worker の登録（workflow・Activity）が
  接続前と同じ、OFF の組み立てが Research のモジュールを**読み込まない**（別プロセスで `sys.modules` を見る。同じ
  プロセスでは他のテストが読み込み済みなので判定できない）、Topic のテンプレートの sha256 と
  `script_input_hash` が f209e7c の値、compose の script-worker が既定 OFF で受け取ること。

## ON（Fake）

- Planner（`tests/unit/test_planner_trend_opt_in.py`）: 実際の Gateway と Trend の実行器（Fake）で完了させた Trend を、
  `fresh` と `stale` の両方で prompt に載せ、版が `topic_en@2+topic_trend_en@1` になること、節が要件の見出しの直前に
  入り他の部分は変わらないこと、要約が観測と仮説を分け URL・依頼 ID を載せないことを見る。読み口の引数（Strategy の
  地域・言語、設定の channel id）も固定する。見出しが 0 個・2 個の prompt には差し込まない（データに見出しの文字列が
  紛れても誤った位置に入れない）。
- 台本の Activity（`tests/unit/test_script_evidence_opt_in.py`）: 実際の Gateway・Evidence の実行器（Fake）・
  `ScriptVerifier` を使い、`ResearchWorkflow` の代わりに実行器をその場で走らせる起動器で、completed → 照合結果が
  Evidence の依頼の成果物として残る（`episode_id` は research 側の参照）、再実行は同じ依頼、partial・blocked
  （Provider `none` では起動もしない）・timeout・主張なし・照合の例外・sha256 の不一致は例外にならず「調査なし」相当を
  返すことを見る。
- 台本の Workflow（`tests/unit/test_script_evidence_workflow_opt_in.py::test_an_on_run_checks_evidence_once_and_always_reaches_script_ready`）:
  照合の結論 3 種・調査なし 2 種・timeout・Activity の失敗のどれでも `script_ready` で終わり、Activity の呼び出しは
  台本の後・`script_mark_ready` の前に 1 回（失敗時は retry の 2 回）。ON の履歴を OFF の worker でも replay する
  （設定を戻しても実行中の台本工程が止まらない）。台本が作れなかった実行では照合しない。

## 境界

- `tests/architecture/test_research_opt_in_boundary.py`: `workers/planning` の中で Research を import してよいのは
  接続の 3 モジュールだけ、Workflow と既存の Activity はそれも Research も import しない、`run_worker.py` は
  接続を関数の中でだけ import する、本番工程は planning の接続を import しない。
- `tests/architecture/test_research_isolation.py::test_the_planning_links_do_not_touch_production_billing_or_artifacts`:
  接続モジュールが本番の課金コードや Artifact・Job・予約のリポジトリに触れない（Episode と Topic Plan の読み取りだけ）。
  照合結果を本番の Artifact にしない（ADR-0038 (c)）ことのコード上の保証。
