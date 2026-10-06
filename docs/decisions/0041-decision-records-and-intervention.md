# ADR-0041: 判断記録と介入（JP-MAS-001 v2.0）を既存の表・台帳・pipeline に重ねる

## Status

Proposed（2026-10-07）。設計 JP-MAS-001 v2.0（[docs/design/JP-MAS-001-v2.0.md](../design/JP-MAS-001-v2.0.md)）の
初期完成範囲（§17）を実装するための決定。契約は [docs/domain/intervention.md](../domain/intervention.md)。

## Context

実査（2026-10-07、`claude/daily-hardening` 80775c0、読み取りのみ）:

- Episode 単位の停止手段が無い。運用は Temporal UI の cancel（`docs/operations/pipeline-worker.md`）。
  本番 workflow に `@workflow.signal/update/query` は 0 件。`EpisodePipelineWorkflow` は子 workflow を待つ間しか
  生きておらず、ADR-0032 は「signal を受ける相手がいない」として signal 方式を却下済み。
- 停止スイッチ（`operational_switches`）はシステム全体だけで、Production（fal）・Script・Storyboard の有料呼び出しを止めない。
- 有料呼び出しと投稿の5経路は全て、外部呼び出し直前に `mark_dispatched` / `mark_upload_dispatched` の
  条件付き UPDATE を commit している（ADR-0013）。台帳は未送信 / 送信済み / 成否不明を区別できる。
- 成果物の「現行」は `artifact_metadata.superseded_at IS NULL`（約10箇所が読む）。採用の履歴・依存の DB 表現は無く、
  上流参照は成果物 JSON の中（`source_script` 等）だけ。
- prompt 全文・生出力の保存は一部（`provider-raw/`）だけ。Topic の LLM は prompt も出力も保存しない。
- API に認証が無く `0.0.0.0:8000` に bind している。
- 1本1,000円・月30,000円の予算を強制するコードは無い（grep で確認）。LLM 費用は記録されていない。
- Temporal Server は 1.29.3（稼働中コンテナで確認）、SDK は constraints 1.32.0 / uv.lock 1.33.0。

## Decision

**判断・介入は PostgreSQL の新しい追記型の表に正本を置き、停止は予約台帳の dispatch の同じ transaction で効かせ、
command は DB の行を outbox として決定的 id の短命 workflow で配送する。pipeline への待機地点は入力フラグで
新方式の Episode にだけ足し、日次実行の履歴と挙動は変えない。**

1. **表**: 新表9（`episode_controls` / `artifact_manifests` / `revision_links` / `agent_runs` / `decision_records` /
   `annotations` / `change_sets` / `intervention_commands` / `episode_events`、migration 0016）。
   revision は既存 `artifact_metadata` の行。`episodes.status` と遷移表は変えず、制御状態は `episode_controls` に分ける。
   行の無い Episode は `legacy`・`RUNNING` として読む（古い Episode の読み取りに migration の backfill は不要）。
2. **停止**: `mark_dispatched` / `mark_upload_dispatched` が同じ transaction で `episode_controls` を `FOR SHARE` で読み、
   `RUNNING` 以外なら拒否（`EpisodeHeldError`）。受付（HOLD_REQUESTED）は API の transaction で書き、
   確定（HELD）は command の適用で、その時点の送信済み・成否不明・未送信を列挙して返す。
3. **配送**: command 行が transactional outbox。API は commit 後に `InterventionCommandWorkflow`
   （id `intervention-{command_id}`）を best-effort で start し、Temporal Schedule `avp-intervention-redelivery` が
   未配送を再 start する。適用 Activity は Episode の制御行を lock して直列化し、終端状態なら保存済み result を返す。
4. **Temporal の message**: Signal / Update を pipeline に足さない。Update の Accepted / Completed の区別は
   command の業務状態（RECEIVED → … → APPLIED）で表す。Workflow Pause（運用機能）は使わない。
5. **待機地点**: `EpisodePipelineInput.control_gate`（既定 False）が True の実行だけ、各工程の子 workflow を起動する前に
   状態 Activity `pipeline_control_gate` を呼ぶ（`workflow.patched("jpmas-control-gate-v1")` の内側）。待機・停止なら
   新しい outcome `parked` で終わり、再開は command（resume / checkpoint 解放）が同じ決定的 id で pipeline を start する。
6. **台本修正と storyboard**: シーン構成・尺が同じ台本修正は storyboard を作り直さず、`revision_links(kind=rebound_to)` で
   storyboard を新しい台本へ結び直す。production / voice / render の「storyboard は現行台本から作られた」検査は
   単一の関数（結び直しを考慮）を経由する。
7. **ログ**: 新イベントは既存の dotted 語彙（`decision.recorded`、`intervention.hold.requested` 等）で
   `contracts/log_contract.py` に足す。正本は `episode_events`、OpenSearch は投影で、`episode_events` から決定的な
   `event_id`（uuid5(episode_id:seq)）で再投影できる。
8. **権限**: 介入 API は設定の principal（token の sha256 → actor・scopes）で認証し、未設定なら無効。
   予算・Schedule・公開設定を変える command 型は作らない。

新しい不変条件 INV-41〜INV-47（[invariants.md](../invariants.md)）をこの ADR で導入し、各々を強制するテストと同じコミットで足す:
- INV-41 ユーザー修正は新 revision として保存し、旧版・コメントを消さない
- INV-42 同じ command_id の再送・復旧は1つの論理変更にしかならない
- INV-43 期待版の不一致は CONFLICT で返し、上書きしない
- INV-44 hold 受付後に新しい dispatch は始まらず、HELD は送信済み・成否不明を列挙する
- INV-45 manifest が変われば、それに結び付いた approval は stale になり流用されない
- INV-46 新方式（mode != legacy）では必須記録の無い有料生成・投稿を開始しない
- INV-47 介入 API は予算・Schedule・公開設定を変更できない

## Alternatives

- **pipeline を長命にして Update で制御**: 子工程の待機中しか生きていない現行 pipeline を作り替えると、稼働中・
  過去の履歴の replay 互換を大きく崩す。Update は Server 1.29.3 で使えるが、API の完了待ちは
  `test_api_no_heavy_work` の禁止事項とぶつかる。採らない。
- **Temporal Workflow Pause**: 動作中の Activity を止めず、pause 中は Query/Update が拒否される（設計 §11）。採らない。
- **イベントソーシング・別のメッセージブローカー**: 設計 §5 が必須にしていない。採らない。
- **`episodes.status` に HELD 等を足す**: 遷移表と既存の全読み手（watchdog・resume plan）を変える。採らない。
- **`superseded_at` だけで採用履歴を持つ**: A→B→A で過去行が復活し、履歴と比較が残らない。採らない。
- **storyboard を作り直して台本修正を反映**: 全画像の入力指紋が変わり、有料素材を全部作り直す（INV-33 の趣旨に反する）。採らない。

## Consequences

良い:
- 停止と送信の競合は DB の行 lock 1つで決まり、機械で検査できる。日次の Episode にも hold が効く。
- 古い Episode・日次実行は表に行を作らずそのまま動く。pipeline の旧履歴は replay で同じ経路を通る。

悪い・引き受けた負債:
- 停止は**有料 dispatch と投稿**だけを止める。ローカルの TTS・render、Topic Planner（Episode 作成前）は止めない。
- 音声の入力指紋が台本全体を含むため、台本修正で全シーンの音声を作り直す（無料のローカル TTS）。
- シーンの並べ替え・尺の変更は初期は適用不可。
- 予算の強制は新方式の ChangeSet 適用時だけ。既存の日次経路の予算強制・LLM 費用の計上は未実装のまま。
- model 識別子は設定値で、Codex CLI が実際に使ったモデル・token usage は未検証（JSONL の usage を解析していない）。
- 再配送 Schedule を1つ増やす（Schedule の作成は `infrastructure/temporal/schedules.py` だけ / INV-2）。
- CANCEL_REQUESTED は受付と記録だけ。Episode の中止は既存の人手手順。
