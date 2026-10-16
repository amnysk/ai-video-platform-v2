# 判断記録と介入（JP-MAS-001 v2.0 の実装契約）

設計の全文は [docs/design/JP-MAS-001-v2.0.md](../design/JP-MAS-001-v2.0.md)（設計ID JP-MAS-001・設計版 2.0）。
この文書は**設計をこの repo の schema・コードへ対応付けた契約**であり、実装と同じコミットで更新する
（AGENTS.md §7）。決定の理由は [ADR-0041](../decisions/0041-decision-records-and-intervention.md)。
作業分担・実装順は別文書 [JP-MAS-001-implementation-plan.md](../design/JP-MAS-001-implementation-plan.md)。

語彙（enum・既定値・上限）の**唯一の宣言元**は `contracts/intervention.py`（AGENTS.md §8）。
DB の CHECK は migration 0016 で literal に凍結する。

## 1. 論理エンティティ → 物理対応

設計 §6 の論理エンティティを、既存の表を優先して対応付ける。新しい表は 9 個で、それぞれ既存の表に
意味を足すと既存の契約（INV-10/11/15/37）を壊すものだけに限った。

| 設計の論理エンティティ | 物理 | 既存 / 新規 | 理由 |
|---|---|---|---|
| Episode／Workspace scope | `episodes`（phase = 既存 `status`）+ `episode_controls`（mode・control_state・制御版・採用 manifest・待機地点） | 既存 + 新規1 | `episodes.status` は phase と停止が混ざった既存語彙で、遷移表（INV/状態遷移の唯一の権威）を変えずに制御軸を足すため。行が無い Episode は `legacy` + `RUNNING` として読む（古い Episode がそのまま読める） |
| ArtifactRevision | `artifact_metadata` の1行 = 1 revision（`id` = revision_id、`sha256` = content_hash、`object_key` = 保存参照、`version`・`schema_version`） | 既存 | 本体は INV-11 で不変。行の可変列（`superseded_at` 等）は「採用ポインタ」の既存実装として残す |
| active_revision_set | `artifact_manifests`（Episode ごとの追記型。`seq`・`members`・`parent_manifest_id`・原因 command）+ `episode_controls.active_manifest_id` | 新規1 | `superseded_at` は「現行」を1本しか表せず履歴を持たない。採用の履歴と比較は追記型の manifest で持つ。既存 worker が読む「現行」（`superseded_at IS NULL`）は**同じ transaction で**一致させる |
| RevisionDependency / parent | `revision_links`（`upstream_revision_id` → `downstream_revision_id`、`kind`） | 新規1 | 既存は成果物 JSON 内の参照（`source_script` 等）だけで、DB で逆引きできない。既存参照は読み取り時に導出でき、新方式の書き込みは明示的に残す |
| AgentRun + ContextSnapshot + ToolInvocation | `agent_runs`（1行 = 1回の役割実行。snapshot・request・response は MinIO に置き参照と sha256 を持つ。tool 呼び出しは `tool_invocations` JSON に列挙し、既存の `provider_reservations.id` / `research_calls.id` を参照） | 新規1 | prompt 全文はログに出せない（INV-39）ので正本は MinIO。課金の正本は予約台帳のまま（二重に持たない） |
| DecisionRecord | `decision_records`（追記型） | 新規1 | 既存 `topic_candidates` は Topic 専用。候補・採否・根拠・留保・対象版を全工程で同じ形にする |
| EvidenceItem | `research_artifacts`（`research_evidence`）を参照 | 既存 | INV-37: 本番の表は research の表を FK で指さない。参照は ID 文字列で持つ |
| Review／Comment／user_constraint／approval | `annotations`（追記型。`kind` で区別。解決・撤回も新しい行） | 新規1 | 4つとも「対象版 + アンカー + actor + 本文」の同じ形。表を分けない |
| ChangeSet／Variant | `change_sets` | 新規1 | 提案の寿命（proposed → applied / rejected / superseded）を command と分ける |
| InterventionCommand + Outbox | `intervention_commands`（command 行そのものが transactional outbox。`dispatched_at` / `dispatch_attempts`） | 新規1 | 設計 §11 の「同一 transaction で outbox」を満たす最小形。別の outbox 表を作らない |
| ReviewCheckpoint | `episode_controls`（`waiting_point`・`waiting_manifest_id`）+ `annotations(kind=approval)` | 新規表なし | 待機は Episode に高々1つ。合格は manifest に結び付いた approval 注記 |
| 判断・介入イベント（event_seq、ログ投影の正本） | `episode_events`（Episode 内で単調増加する `seq`、`event_name`、参照、`projected_at`） | 新規1 | OpenSearch は投影。再投影の元を DB に持つ（設計 §14/§15） |
| Measurement／Lesson | 初期範囲外（`analytics_snapshots` を参照で読むだけ） | — | 設計 §17 の初期範囲に含まれない |

新表: `episode_controls` / `artifact_manifests` / `revision_links` / `agent_runs` / `decision_records` /
`annotations` / `change_sets` / `intervention_commands` / `episode_events`（migration 0016）。

### 共通項目
全ての新表の行は `schema_version`・`created_at`・`actor_type`／`actor_id`（該当するもの）・`correlation_id` を持つ。
`actor_type` は `human` / `ai_assistant` / `agent` / `system`。**AI の提案を人の直接編集として記録しない**:
AI アシスタントが作った ChangeSet を人が採用したら、ChangeSet の `actor_type=ai_assistant`、適用 command の
`actor_type=human` として両方を残す。

## 2. 制御の状態（phase と control_state の分離）

phase は既存の `episodes.status`（遷移表 [state-transitions.md](./state-transitions.md) は**変えない**）。
制御は `episode_controls.control_state`:

| From | To | 契機 | 主体 |
|---|---|---|---|
| （行なし = RUNNING） | `HOLD_REQUESTED` | request_hold の受付（API の transaction 内） | API |
| `RUNNING` | `HOLD_REQUESTED` | 同上 | API |
| `WAITING_REVIEW` | `HOLD_REQUESTED` | 同上（待機中の停止は保留へ） | API |
| `HOLD_REQUESTED` | `HELD` | command の適用時、dispatch gate と同じ行 lock の下で確定 | command worker |
| `RUNNING` | `WAITING_REVIEW` | pipeline の control gate が待機地点に到達 | pipeline の状態 Activity |
| `HELD` / `WAITING_REVIEW` | `RUNNING` | resume の適用 / checkpoint の解放 | command worker |
| 任意 | `CANCEL_REQUESTED` | cancel の受付（初期は受付と記録のみ。Episode の中止は既存の人手手順） | API |

- **HOLD_REQUESTED は「追加課金がなくなった」ではない。** 受付の時点で dispatch gate は新しい送信を拒否するが、
  既に dispatch 済みの provider job は完了・課金されうる。`HELD` の応答は、その時点で送信済み・成否不明の
  予約を列挙して返す（§5）。
- **待機地点の実装（pipeline の control gate）**: `EpisodePipelineInput.control_gate=True` の実行だけが、
  各工程の起動前に Activity `pipeline_control_gate`（`PipelineActivities.control_gate` →
  `gate.evaluate_stage_gate`）を呼ぶ。呼び出しは `workflow.patched("jpmas-control-gate-v1")` の内側にあり、
  既定（`False`。日次 Schedule・旧履歴）は patch の marker も Activity も履歴に足さない。
  gate は `running` 以外なら起動せず、この工程の前が有効な待機地点で現行 manifest への approval が無ければ
  `WAITING_REVIEW` にして `checkpoint.waiting` を**同じ commit** で記録する。起動しないとき pipeline は
  失敗ではなく `parked` で終わり、再開は resume / checkpoint 解放の command が子 pipeline を start する。
  待機中の再評価（Activity の retry・再実行）は状態が既に `WAITING_REVIEW` なので `checkpoint.waiting` を
  重ねて書かない。
- 待機期限で自動承認しない。期限の扱いは `policy_version` に記録した明示方針（`keep_waiting` だけを初期実装）。
- Temporal の Workflow Pause は使わない（設計 §11、ADR-0041）。`HELD` / `WAITING_REVIEW` はアプリの状態。

### 運転モード（`episode_controls.mode`）
| mode | 振る舞い | 既定 |
|---|---|---|
| `legacy` | 従来どおり。判断記録の必須化なし、待機地点なし。hold は受け付ける（dispatch gate は全 Episode に効く） | 行の無い Episode・日次実行 |
| `auto_intervenable` | 自動で進む。判断記録を必須にする（欠けたら有料送信・投稿を開始しない） | — |
| `review_points` | `review_points` に列挙した地点だけ `WAITING_REVIEW` | — |
| `co_production` | 主要地点（script 後・有料素材生成前・投稿前）すべてで待つ | 新方式の試験 Episode |

日次 Schedule の入力・既定は変えない（日次 Episode は `legacy`）。mode の変更は人の権限だけ（AI 不可）。

## 3. dispatch gate（停止と送信の競合）

有料 provider と投稿の5経路（script / storyboard / scene-alternative / paid_job / upload）は、外部呼び出しの直前に
`ProviderReservationRepository.mark_dispatched` / `mark_upload_dispatched` の条件付き UPDATE を commit する。
gate はこの**同じ transaction** で `episode_controls` 行を `SELECT … FOR SHARE`（PostgreSQL。SQLite は
DB 全体の書き込み直列）で読み、`control_state != RUNNING` なら更新せず `EpisodeHeldError` を送出する。
hold の受付は同じ行を `UPDATE`（行 lock）するので、

- hold の commit より前に commit した dispatch → 「送信済み（in-flight）」として HELD 応答に列挙される
- hold の commit より後の dispatch → gate が拒否する。予約は `reserved` かつ `dispatched_at IS NULL`（未送信）のまま残り、
  resume 後に同じ冪等キーで再利用される（新しい予約・二重課金を作らない）

の2通りしかない。`episode_controls` 行の無い Episode は gate が `RUNNING` として通す（行を作らない）。
`mode != legacy` の Episode では、gate は加えて**必須記録**（§6）の有無を検査し、欠けていれば
`MissingDecisionRecordError` で拒否する（有料生成・投稿を開始しない）。

## 4. command の寿命と配送

```
API（1 transaction）:  intervention_commands INSERT(status=RECEIVED, dispatched_at=NULL)
                       [+ request_hold なら episode_controls.control_state=HOLD_REQUESTED]
                       [+ episode_events INSERT]
           commit → 202 {command_id, status: RECEIVED}
配送:      API が commit 後に best-effort で InterventionCommandWorkflow(id=intervention-{command_id}) を start
           + 再配送 Schedule（avp-intervention-redelivery）が、dispatched_at IS NULL / 停滞した RECEIVED・VALIDATED・APPLYING と、
           「APPLIED だが result.pipeline_start に対する pipeline が未記録、または outcome=start_failed」の command を再 start
           （Schedule は所有者が `scripts/ensure-daily-schedule.py --intervention --apply` で登録する。worker は自動登録しない）
Workflow:  apply Activity（1 transaction・episode_controls 行 lock で Episode 内を直列化）
             status が終端なら保存済み result を返す（再配送・復旧の冪等）
             RECEIVED → VALIDATED（権限・期待版・予算・制約）→ APPLYING → APPLIED | CONFLICT | REJECTED | FAILED
             状態変更・新 revision・manifest・判断記録・episode_events を同じ commit に入れる
           resume / checkpoint 解放なら、start の前に check Activity が「適用の後に状況が変わっていないか」を見る
             （適用時に記録した control_revision と現在が一致・control_state=RUNNING・Episode が投稿済み/終端でない）。
             変わっていれば start せず outcome=not_needed（reason 付き）。再配送が終わった pipeline を再起動しない
           子 EpisodePipelineWorkflow を決定的 id で start（既存の二重起動防止 / INV-30）
             agentic Episode（`pin_workflow_plan` が pin した計画がある）は、子を `AgenticEpisodeWorkflow`
             （id `agentic-episode-{id}`、ABANDON）にする。**判定は apply Activity（I/O 側）**が行い、結果に
             `pipeline_kind=agentic` と `AgenticEpisodeInput` 形の `pipeline_input` を載せる。workflow はその値で
             分岐するだけで、`workflow.patched("agentic-resume-starts-agentic-episode-workflow")` が旧履歴の replay
             を守る（旧い結果には `pipeline_kind` が無く legacy。コマンド列は不変 / INV-55）。閉じた実行（parked・完了・
             失敗）の id は再利用できる（既定の ALLOW_DUPLICATE）。実行中なら already_running。再起動は同じ pin 済みの
             計画を上から歩き直す（完了済みの agent stage は保存済みの結果を返し、LLM・予約を増やさない。upload gate は維持）
             **worker の `JPMAS2_AGENTIC_WORKFLOWS` が OFF のとき**、固定計画を持つ Episode の再開は check Activity が
             `proceed=false, reason=feature_disabled` を返し、`not_needed` / `feature_disabled` を記録して何も start しない
             （AgenticEpisodeWorkflow も legacy の pipeline も。workflow のコードは変えない。ADR-0044 §3-3）。
             子 workflow の task queue と入力は API の再開・CLI と同じ関数（`agentic_episode_start_spec`）が作る
           follow-up Activity が結果を必ず command result.pipeline に追記する:
             outcome ∈ started / already_running / start_failed / not_needed（contracts の PipelineStartOutcome）
```

- 同じ `command_id` で**同じ payload**（`payload_sha256` 一致）の再送は既存の行を返す（新しい行も適用も作らない）。
  **違う payload** は `409 command_id_reused`。
- `expected_control_revision`（Episode の制御版）・`expected_target_revision`（対象成果物の revision_id）が
  現在値と違えば `CONFLICT`。差分は再取得させ、上書きしない。
- `RECEIVED` を「修正完了」と表示しない。外部生成を伴う場合、command の `APPLIED` と完成動画の生成完了は別
  （ReviewPacket は両方を別欄で出す）。

`InterventionCommandWorkflow` の返す `replayed` は **apply Activity が保存済み result を返したか**
（再配送・復旧では `true`）。follow-up の追記は再適用ではないので、その値で上書きしない。
再配送 Schedule（`avp-intervention-redelivery`、60 秒間隔・overlap SKIP）は
`InterventionRedeliveryWorkflow` を start し、未配送・停滞した command ごとに
`InterventionCommandWorkflow`（id `intervention-{command_id}`）を子として start する（実行中なら束ねる）。
command worker は専用の task queue（`intervention`）で動く。

### 注記の解決（`resolve_annotation`）
scope `comment`（AI も `comment` を持てば可。追加の権限は無い）。`comment` / `review_finding` 注記を解決または
「直さない」と宣言する（`resolution` ∈ `resolved` / `wont_fix`、任意の `note`）。適用は `annotations` に
`kind=resolution`（`refers_to_annotation_id` = 対象、`payload.resolution`）を**追記**し、同じ transaction で
`review.resolved` を `episode_events` に記録する。元の注記は変えず消さない（INV-41）。同じ `command_id` は
1行・1 event（INV-42。`annotations.command_id` の一意）。拒否（REJECTED）: 対象が無い・他 Episode =
`not_found`、対象が comment / review_finding でない = `invalid_payload`、解決済み = `invalid_state`。
read model の `status=resolved` と ReviewPacket の「未解決コメント」（`open_comments`）は、この行を読む。

### クラッシュ・重複の境界
| 落ちる地点 | DB に残るもの | 復旧 |
|---|---|---|
| API commit 前 | 何も無い | クライアントが同じ command_id で再送 |
| API commit 後・workflow start 前 | RECEIVED・未配送 | 再配送 Schedule が start |
| workflow start の重複 | — | 決定的 workflow id で Temporal が1つに束ねる |
| apply の commit 前 | RECEIVED/VALIDATED（commit されない中間は残らない） | Activity retry が最初からやり直す |
| apply の commit 後・Workflow が結果を受け取る前（Worker 障害） | APPLIED + result | Activity retry / 再配送が同じ command_id を見て**保存済み result を返す**（再適用しない） |
| pipeline start の後・follow-up 前 | APPLIED、pipeline は走っている | 再配送が拾い、start は `WorkflowAlreadyStartedError` → already_running として記録 |
| APPLIED の後・start の前に workflow が落ちた | APPLIED + `pipeline_start`、`pipeline` 無し | 再配送が拾い、check を通れば pipeline を1回だけ start。記録後は拾われない |
| start の失敗 | `pipeline.outcome=start_failed` | 再配送が再試行する（check は毎回通る） |
| 適用の後に hold・待機・終端になった | APPLIED + 制御版が進んだ | check が `not_needed` を記録し、何も start しない |
| 固定計画を持つ Episode で worker の agentic flag が OFF | APPLIED + `pipeline_start` | check が `not_needed`（reason `feature_disabled`）を記録し、何も start しない |

`dispatched_at` / `dispatch_attempts` は、再配送が子 workflow の start に成功した（または既に走っている）ときだけ
記録する。列挙しただけでは増やさない。

## 5. 停止中にできること・HELD の応答
停止中も: 読み取り、コメント、ChangeSet の作成、送信済み provider job の状態照会と結果保存（既存の await 経路は gate を
通らない）、費用の精算、履歴の記録。`HELD` の応答（command result）には次を含める:
`in_flight`（`dispatched_at` あり・未精算の予約）、`unknown_outcome`（dispatch 済み・job 参照なし）、
`undispatched`（予約済み・未送信。課金されない）。成否不明は自動で再送も解放もしない（INV-15）。

## 6. 判断記録と必須記録
- `agent_runs`: 役割（`producer` / `writer_director` / `reviewer`）、model 識別子（設定値。CLI の実モデルは未検証と明記）、
  `prompt_version`、ContextSnapshot（実際に渡した入力・参照した成果物 revision の固定版。MinIO
  `agent-runs/{episode_id または topic_plan_id}/{agent_run_id}/context-{sha256 先頭16}.json` と sha256。
  名前に内容の sha256 を含めるのは、Activity の再実行で内容が変わっても不変性（INV-11）に当たらないため）、
  request／response の保存参照（同じ接頭辞の `request-…json` / `response-…json`）、
  `tool_invocations`（予約 ID・research call ID を参照）、token usage（取得できなければ NULL。0 で埋めない）。
  捕捉は `infrastructure/intervention/capture.py`。**`mode != legacy` の Episode だけ**が記録し（行の無い
  Episode・日次は何も書かない）、記録できなければ Activity を `needs_input` で失敗させる（成果物の
  `artifact_metadata` と agent_run・判断記録は同じ transaction）。再開（保存済みの生出力から続行）では
  prompt を同じ入力・同じ版から組み直して `prompt_source=re_rendered` と記録し、組み直せない工程
  （storyboard の prompt は adapter の内側）は `unavailable` と記録する（補完しない）。
  Topic Planner（producer）は Episode より前に走るので mode では切り替えず、設定
  `TOPIC_PLANNER_CAPTURE_ENABLED`（既定 OFF）が ON のときだけ記録する。候補生成の snapshot を
  `agent-runs/_staging/topic/{日}/{strategy}/{content}/r{round}/{候補の sha256 先頭16}.json` に置き、plan を
  保存する transaction で `agent-runs/{topic_plan_id}/…` の agent_run と `choose_topic`（保存済み候補から。
  補完しない）にする。全候補が却下された round の一時置き場は行にならず残る（plan が無いため）。
  compose の環境変数への受け渡しは未配線（`compose.yaml` は担当外）。
- Episode・Scene・Workflow への結び付き（migration 0018、全て NULL 可）: `agent_runs.workflow_id` /
  `workflow_run_id` / `activity_id`（その実行を作った Activity の Temporal 識別子。`temporalio.activity.info()` から
  **Activity の内側でだけ**取る。test・CLI・Topic の staging など外側は NULL）、`decision_records.scene_id`
  （payload が単一 Scene を明示するときだけ。現状は `scene.override_visual` の適用。`script.edit_scenes` は
  segment 単位で Scene を特定しないので NULL。推測しない）と `workflow_id` / `workflow_run_id`（同じ取り方）。
  既存行は書き換えない。Activity の入力の形は変えない（replay 互換）。読み出しは explain / read_model / ReviewPacket
  と `search_decisions` の `scene_id` 絞り込み、ログは既存の `workflow_id` / `run_id` / `scene_id` 項目に載せる。
- `decision_records`: 問い・実際の候補（無ければ1件）・選択・`rationale_kind`（`reported` / `post_hoc_summary` /
  `human` / `rule`）・根拠参照・留保・制約・対象 revision・結果 revision・評価の種別（`self_assessed` / `measured` / `rule`）。
  **候補・スコアを補完しない。** 入力 snapshot に無い資料を根拠に挙げたら `evidence_check=unverified_ref` を付ける。
- `mode != legacy` の必須記録: 現行の script・storyboard の各 revision に、生成した agent_run か、
  人の変更を表す decision_record が結び付いていること。

## 7. ChangeSet と影響範囲（初期の対応範囲）
| 変更の種類 | 新 revision | stale にするもの | 再生成 | 再利用 |
|---|---|---|---|---|
| `script.edit_scenes`（ナレーション・字幕文・題・hook。シーン構成と尺は不変） | script | 音声・manifest・完成動画・それらへの approval | 音声（ローカル TTS）・manifest・render | 画像・動画（storyboard を作り直さない。`revision_links(kind=rebound_to)` で storyboard を新台本へ結び直す） |
| `scene.override_visual`（1シーンの映像の差し替え） | `scene_visual_override`（既存 ADR-0035 の型） | そのシーンの画像・動画・manifest・完成動画・approval | そのシーンの画像・動画・manifest・render | 他シーン（INV-33） |
| シーンの並べ替え・尺の変更 | 初期は**適用不可**（REJECTED `unsupported_change`）。storyboard の再生成が全シーンの画像を作り直す（`storyboard_sha256` が全画像の入力指紋に入る）ため、見積りと共に人が別手順で判断する | — | — | — |

適用の規則（担当 B の実装）:
- `script.edit_scenes` の `edits[].visual`（映像の記述）は**適用不可**（REJECTED `unsupported_change`。
  `scene.override_visual` を案内する）。画像・動画は storyboard から作られ、台本の `visual` を直しても
  どの画像も変わらない（黙って「直ったように見える」command を作らない）。
- 承認（approval）が有効かの判定は `gate.approval_matches`（manifest 一致・撤回されていない・指定があれば
  待機地点一致）の1か所。read model の `approval_is_current` と変更の再レビュー対象の算出は、それに委ねる
  （AGENTS.md §8）。
- 「stale にする」は**現行から降ろす**こと（`ArtifactMetadataRepository.retire_current`。行は消さない）。採用 manifest は
  常に現行と一致し、resume は現行に出力の無い最初の工程から pipeline を走らせる。
- 適用は工程が走っていない間だけ（Episode が `in_progress` なら `invalid_state`）。投稿済み・終端の Episode は不可。
- 送信済み（成否未確定）の有料 job があるシーンの `scene.override_visual` は `invalid_state`。旧版の入力で送った
  結果が差し替えの後に届くと、画像・動画の Activity は入力の不一致で保存を拒み課金済みの結果が宙に浮くため、
  結果が旧版の revision として保存されてから適用する（旧版は新しい manifest に入らない）。
- 台本の修正は台本工程の再利用キー（`input_hash`）を引き継ぐ（台本工程の再実行が人の修正を上書きしない）。
- 人の差し替えは `scene_visual_override` に `change_set_id` を持ち、`rejection_ids` は空（ADR-0035 の代替案は1件以上）。

既知の制限: 音声の入力指紋は台本全体の sha256 を含むので、`script.edit_scenes` は**全シーンの音声**を作り直す
（無料のローカル TTS。有料素材は作り直さない）。音声単位の再利用は別 ADR。

## 8. 権限
API の command・read は `INTERVENTION_PRINCIPALS`（設定。token の sha256 → principal）で認証する。未設定なら
介入 API 全体が `503 intervention_disabled`。principal は `actor_type`・`actor_id`・`scopes`
（`read` / `comment` / `propose` / `hold` / `apply` / `resume` / `set_mode`）を持ち、command 行に実際に使った scope を
記録する。**予算上限・Schedule・公開設定を変える command 型は存在しない**（受け付けると 422 `unsupported_command`）。
`set_mode` は `actor_type=human` の principal にだけ付与できる（設定読み込み時に検査）。

**AI（`ai_assistant` / `agent`）は有料の生成を始められない。** `apply_change` は 0 円・制約に触れない変更だけ
（有料は `forbidden` / `detail=ai_paid_apply`）。`resume` と `release_checkpoint` は降ろされた出力を作り直す
ので、これから走る実行が有料の生成（現行の storyboard のシーンに画像か動画が無い、または storyboard から
やり直す）を伴うなら AI には REJECTED `forbidden` / `detail=ai_resume_requires_paid_generation` とし、
制御状態は変えず pipeline も始めない。判定は `resume` が開始工程を決めるのと同じ「現行に何が無いか」
（`paid_generation_required`）。音声・manifest・render だけの再開は AI にも可。人は影響を受けない。
`forbidden` の原因は `result.detail`（`contracts.intervention.ForbiddenDetail`: `scope_mismatch` /
`human_only` / `ai_paid_apply` / `ai_resume_requires_paid_generation`）で区別する。

## 9. 予算
上限は `EPISODE_BUDGET_JPY`（既定 1000）・`MONTHLY_BUDGET_JPY`（既定 30000）・換算 `BUDGET_USD_JPY_RATE`（既定なし）。
支出は予約台帳（`provider_reservations.estimated_cost_usd`、reserved + spent）と `research_calls` から数え、
LLM（Codex）の費用は記録が無いので `unmeasured` として別に示す（0 として足さない）。
有料の見積りを伴う ChangeSet は、換算が未設定・残予算不足なら `REJECTED budget_unverifiable` / `budget_exceeded`。
有料の見積りは、そのシーンの画像・動画を前回送ったときに adapter が予約台帳へ書いた `estimated_cost_usd`
（fal adapter を介入のコードから import しない / INV-18）。材料が無ければ見積り不明で `budget_unverifiable`。
**既存の日次経路に予算の強制は無い**（調査で確認。今回は新方式の適用時の検査だけを足す。全経路の強制は未実装として報告）。

## agentic Episode の resume（ADR-0043 §Consequences の解消）

介入 command の resume と `POST /episodes/{id}/resume`（ADR-0032）は同じ決定をする。Episode に pin 済みの agentic 計画が
あれば `AgenticEpisodeWorkflow` を（再）起動し、無ければ従来の `EpisodePipelineWorkflow`。
API 側は `infrastructure.agents.pinned_plan.read_pinned_agentic_template`（metadata の行が無ければ ArtifactStore に
触らない）で判定し、`WorkflowStarter.start_agentic_workflow`（`ALLOW_DUPLICATE`）で起動する。最初の起動
（`TemporalAgenticStarter.start`）は REJECT_DUPLICATE のまま（完了済みの Episode を再実行しない）。
`PipelineKind` / `pipeline_kind` の定義は `contracts/agentic_pipeline.py` の1か所。AGENTS.md §8 の grep
（`grep -rn "pipeline_kind\|PipelineKind\|AGENTIC_RESUME_PATCH_ID" apps/ workers/ domain/ infrastructure/ contracts/ docs/`）:
変更前 0 件、変更後は宣言 `contracts/agentic_pipeline.py`（書き手・宣言）、読み手 `workers/intervention/workflows.py`、
書き手 `workers/intervention/activities.py`。無関係 0 件。
