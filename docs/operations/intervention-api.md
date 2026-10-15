# 介入 API（/v1/intervention）の HTTP 仕様と使い方

設計 [JP-MAS-001 v2.0](../design/JP-MAS-001-v2.0.md) §10、契約 [docs/domain/intervention.md](../domain/intervention.md)、
決定 [ADR-0041](../decisions/0041-decision-records-and-intervention.md) §8、不変条件 INV-47（[invariants.md](../invariants.md)）。
実装は `apps/api/routers/intervention.py`（HTTP）・`apps/api/auth.py`（principal）・`apps/api/intervention_backend.py`
（store と担当 A / B の関数への接続）。

**この API が変えられないもの（INV-47）**: 予算上限（`EPISODE_BUDGET_JPY` / `MONTHLY_BUDGET_JPY`）・Schedule・
YouTube の公開設定（private 固定 / INV-19）。これらを変える route も command 型も存在しない。所有者が設定・
既存の手順で変える。

## 1. 有効化（既定は無効）

`INTERVENTION_PRINCIPALS` が**未設定なら介入 API の全 route が `503 intervention_disabled`**。API は `0.0.0.0:8000` に
bind しているので、token を発行するまで何も開かない。

値は **token の sha256（小文字 hex）→ principal** の JSON、またはその JSON ファイルのパス（`{` で始まらなければパス）。
平文の token は設定・DB・ログのどこにも置かない。

```json
{
  "<sha256 of the owner's token>": {"actor_type": "human", "actor_id": "owner",
                                    "scopes": ["read", "comment", "propose", "hold", "apply", "resume", "set_mode"]},
  "<sha256 of the assistant's token>": {"actor_type": "ai_assistant", "actor_id": "chat-assistant",
                                        "scopes": ["read", "comment", "propose"]}
}
```

token の作り方と hash（token はシェル履歴に残さない）:

```bash
python -c 'import secrets; print(secrets.token_urlsafe(32))' > ~/.avp-intervention-token && chmod 600 ~/.avp-intervention-token
printf %s "$(cat ~/.avp-intervention-token)" | sha256sum | cut -d' ' -f1   # これを JSON の key にする
```

- `actor_type`: `human` / `ai_assistant` / `agent`（`system` は内部の主体なので不可）。
- `scopes`: `read` / `comment` / `propose` / `hold` / `apply` / `resume` / `set_mode`。command 型ごとの必要 scope は
  `contracts.intervention.REQUIRED_SCOPE`（`comment`・`set_user_constraint`・`resolve_annotation` → comment、`request_hold`・`request_cancel`
  → hold、`propose_change` → propose、`apply_change`・`reject_change`・`release_checkpoint` → apply、`resume` → resume、
  `set_mode` → set_mode）。読み取りは全て `read`。
- **AI（`ai_assistant` / `agent`）に人専用 scope（`HUMAN_ONLY_SCOPES` = `set_mode`）を与えた設定は読み込まない**。
  その場合や、JSON・hash・scope が不正な場合は `503 intervention_misconfigured`（ログに理由。hash・token は出さない）。
  API の他の route（`/episodes` 等）は止めない。
- compose の `api` サービスは現在この変数を渡していない（= 本番は無効のまま）。有効にするには所有者が
  `api` の environment に `INTERVENTION_PRINCIPALS`（ファイルを mount してそのパスを渡すのが望ましい）を足す。

## 2. 認証・エラーの形

全 route で `Authorization: Bearer <token>`。エラーは FastAPI の `{"detail": {...}}` の中に機械可読な `error`:

| status | `detail.error` | 意味 |
|---|---|---|
| 503 | `intervention_disabled` / `intervention_misconfigured` | 未設定 / 設定を読み込めない |
| 503 | `intervention_unavailable` | 担当 A / B のモジュールを組めない（import の失敗等。次の要求で再試行する）。store だけで読める route（`decisions`・`commands`・`change-sets`）は動き続ける |
| 401 | `unauthenticated` | token が無い・違う（`WWW-Authenticate: Bearer`） |
| 403 | `missing_scope`（`required_scope` 付き） | principal に必要な scope が無い |
| 404 | `episode_not_found` / `command_not_found` / `change_set_not_found` / `revision_not_found` | 対象が無い・その Episode のものでない |
| 403 | `forbidden` | 受付が拒否した（principal がその command を出せない） |
| 409 | `command_id_reused` | 同じ `command_id` が**違う内容**、または**違う主体**で既に使われた（誰のものかは返さない） |
| 409 | `stale_control_revision` / `stale_target_revision` / `invalid_state` | 受付の拒否のうち競合（期待版が古い・今の状態では出せない）。読み直して出し直す。行は作られない |
| 422 | `unsupported_command` | command 型が語彙に無い（予算・Schedule・公開設定を含む全ての未知の型） |
| 422 | `invalid_payload`（`errors` 付き。入力値は返さない） | envelope・payload の形が違う／余分な項目 |
| 422 | `episode_mismatch` | 変更案ファイルが別の Episode のもの |
| 422 | `revision_type_mismatch` | 違う型の revision 同士は比較できない |
| 422 | `unsupported_change` / `user_constraint_violation` / `budget_exceeded` / `budget_unverifiable` / `command_rejected` | 受付の拒否（`RejectReason`）。`command_rejected` は未知の理由。行は作られない |

受付の拒否（B の `CommandReceiveError`）の `detail.error` は `RejectReason` の値そのもの。`message` は固定文で、入力値・
例外文・token は返さない。変更案の取り込み（`change-proposals`）で1件でも拒否されれば全件を巻き戻す。

## 3. 読み取り（scope `read`）

| method / path | 返すもの |
|---|---|
| `GET /v1/intervention/episodes/{episode_id}` | Episode の read model（phase・制御状態・採用 manifest・成果物・stale 等。形は担当 A の `EpisodeView`） |
| `GET /v1/intervention/episodes/{episode_id}/decisions` | `{"decisions": [...]}`（`decision_records`。候補・選択・根拠・`rationale_kind`） |
| `GET /v1/intervention/episodes/{episode_id}/revisions/compare?a={revision_id}&b={revision_id}` | 2つの revision の比較（担当 A の `compare_revisions`）。両方がその Episode の revision でなければ 404 |
| `GET /v1/intervention/episodes/{episode_id}/review-packet?format=json\|markdown\|html` | ReviewPacket。`json`（既定・機械向け）、`markdown`（`text/markdown`・人向け）、`html`（Markdown をエスケープして `<pre>` に入れた読み取り専用の簡易表示。CSP `default-src 'none'`）。全て `Cache-Control: no-store` |
| `GET /v1/intervention/episodes/{episode_id}/commands` | `{"commands": [CommandView...]}`（受付順） |
| `GET /v1/intervention/commands/{command_id}` | `CommandView`（下記） |
| `GET /v1/intervention/episodes/{episode_id}/change-sets` | `{"change_sets": [ChangeSetView...]}` |
| `GET /v1/intervention/episodes/{episode_id}/change-sets/{change_set_id}` | `ChangeSetView`: 変更案（`proposal` = 差分の内容）・影響範囲（`impact`: stale・再生成・再利用）・見積り（`estimated_cost_usd`。**未見積りは `null` と `cost_estimated: false`。0 と表示しない**）・状態・提案者 |

`CommandView`:

```json
{"command_id": "…", "episode_id": "…", "type": "comment", "status": "received",
 "status_text": "受付済み・未適用（適用は非同期。status で確認する）",
 "applied": false, "terminal": false,
 "actor": {"type": "ai_assistant", "id": "chat-assistant"}, "granted_scope": "comment",
 "expected_control_revision": null, "expected_target_revision": null, "payload": {…},
 "reject_reason": null, "result": null, "received_at": "…", "finished_at": null, "dispatched": true}
```

**`received` は「完了」ではない。** `applied` が `true` になるのは `status == "applied"` だけ。`status_text` は
`applied` 以外を全て「未適用」と書く。外部生成を伴う変更では、command の `applied` と完成動画の生成完了は別
（ReviewPacket で別欄）。

## 4. command（POST）

### `POST /v1/intervention/episodes/{episode_id}/commands`
body は `contracts.intervention.CommandEnvelope`:

```json
{"command_id": "8〜64文字。クライアントが生成（uuid4 推奨）",
 "type": "comment | resolve_annotation | set_user_constraint | request_hold | request_cancel | propose_change | apply_change | reject_change | resume | release_checkpoint | set_mode",
 "expected_control_revision": null, "expected_target_revision": null,
 "payload": {…型ごとの payload（PAYLOAD_MODELS。余分な項目は 422）…},
 "correlation_id": null}
```

| 応答 | 条件 |
|---|---|
| `202` + `CommandView`（`status: received`） | 新しい command。DB に commit 済み。`InterventionCommandWorkflow` を commit 後に start（best-effort。失敗はログに残り、再配送 Schedule が拾う） |
| `200` + 保存済みの `CommandView` | 同じ `command_id`・同じ内容の再送（新しい行も適用も作らない）。ネットワーク断の後は**同じ `command_id` で再送する**のが安全 |
| `409 command_id_reused` | 同じ `command_id`・違う内容 |
| `403 missing_scope` / `404 episode_not_found` / `422 unsupported_command` / `422 invalid_payload` | 上表 |

`resolve_annotation`（scope `comment`）の payload は `{"annotation_id", "resolution": "resolved | wont_fix", "note"?}`。
対象は同じ Episode の `comment` / `review_finding` 注記。適用は受付の transaction で済み、`resolution` 注記を**追記**して
`review.resolved` を記録する（元の注記は変えない）。対象が無い・他 Episode は `not_found`、種類が違えば
`invalid_payload`、解決済みなら `invalid_state`（いずれも REJECTED）。

handler は検証 → 保存（1 transaction）→ start → 返すだけで、適用を待たない（INV-16）。適用の結果は
`GET /v1/intervention/commands/{command_id}` で見る。`request_hold` は受付の transaction で `HOLD_REQUESTED`
（以降の新しい有料送信・投稿は止まる。既に送信済みのものは止まらない。`HELD` の応答で列挙される。
[intervention.md](../domain/intervention.md) §2/§5）。

### `POST /v1/intervention/episodes/{episode_id}/change-proposals`（scope `propose`）
ReviewPacket を読んだ人・AI が作った変更案ファイル（担当 A の `contracts.review_packet.ChangeProposalFile`。
`proposals` は `ProposeChangePayload` の列、`episode_id` を持つなら URL と一致すること）を、案ごとの
`propose_change` command に変える。**全ての案を1 transaction で受け付ける**（全部か無し）。

command_id は `cp-` + sha256(`episode_id`・案の正規化 JSON・同じ内容の案の何個目か) の決定的な値なので、
同じファイルを再度取り込んでも新しい command は作られない（`200`、各要素 `created: false`）。並び順を変えても同じ id。

```json
{"commands": [{"command_id": "cp-…", "status": "received", "status_text": "…", "created": true}]}
```

### `POST /v1/intervention/episodes/{episode_id}/storyboard/proposals`（scope `propose`、ADR-0043）
絵コンテの映像文の変更案を、edit（1〜12件）ごとの `propose_change`（`scene.override_visual`）にする。
**`JPMAS2_APPLICATION_API=true` のときだけ有効**（既定は `404 feature_disabled`）。本文は
`contracts.application.ProposeStoryboardChangeRequest`（`command_id` 8〜64 文字、`expected_control_revision?`、`proposal`）。
`proposal.episode_id` は URL と一致すること。`visual_description` 以外（`framing` `camera_movement` `visual_kind`
`reference_asset_id`）を含む edit は `422 unsupported_change`。scene_id の重複は `422 invalid_payload`。
command_id は `uuid5(NS, "{command_id}:{index}")` なので同じ要求の再送は同じ command 群（`200`、`created: false`）、
同じ `command_id` で別内容は `409 command_id_reused`。適用はしない（`apply` は別操作）。応答は
`{"request_command_id", "episode_id", "commands": [{"scene_id", "command_id", "status", "created"}]}`。
詳細は [application.md](../domain/application.md)。

### application 層の操作（ADR-0044、`JPMAS2_APPLICATION_API=true` のときだけ。既定は `404 feature_disabled`）
method・path・body・応答の型は `contracts/application_operations.py:OPERATIONS` が唯一の宣言元で、route はそこから
登録される。OpenAPI 断片は `docs/api/openapi.application.json`（`python scripts/export-openapi.py --application`）。
scope・人限定の検査は Application Service が行う。`X-Correlation-ID`（`[A-Za-z0-9._:-]{1,128}`）があれば相関 ID に使い、
無ければ生成する。

| 操作 | method path（`/v1/intervention` から） | 成功 |
|---|---|---|
| `create_episode_draft` | `POST /episodes/drafts` | `201`（再送 `200`） |
| `list_episodes` | `GET /episodes?limit=&before=` | `200` |
| `get_episode_workspace` | `GET /episodes/{id}/workspace` | `200` |
| `start_agentic_production`（人のみ） | `POST /episodes/{id}/production` | `202`（既に開始済み `200`） |
| `request_hold` | `POST /episodes/{id}/hold` | `202`（再送 `200`） |
| `resume_production` | `POST /episodes/{id}/resume` | `202`（再送 `200`） |
| `get_storyboard` | `GET /episodes/{id}/storyboard` | `200` |
| `propose_storyboard_change` | `POST /episodes/{id}/storyboard/proposals` | `202`（再送 `200`） |
| `preview_change_impact` | `GET /episodes/{id}/change-sets/{change_set_id}/impact` | `200` |
| `apply_approved_change` | `POST /episodes/{id}/change-sets/{change_set_id}/apply` | `202`（再送 `200`） |
| `get_agent_decisions` | `GET /episodes/{id}/agent-decisions` | `200` |
| `get_quality_review` | `GET /episodes/{id}/quality-review` | `200` |
| `get_workflow_progress` | `GET /episodes/{id}/progress` | `200` |

失敗は `{"detail": {"error": <OperationErrorCode>, "message", ...}}`。status: `409`（`command_id_reused`・
`stale_*`・`invalid_state`）、`403`（`missing_scope`・`forbidden`）、`404`（`episode_not_found`・
`change_set_not_found`・`feature_disabled`）、他は `422`。UI の origin は `API_CORS_ORIGINS`（カンマ区切り、
既定は空 = CORS 無し、`*` は起動時に拒否）。1 操作 = `application.operation.completed` のログ 1 行。

## 5. 典型的な流れ（curl）

```bash
export AVP_INTERVENTION_URL=http://localhost:8000
export AVP_INTERVENTION_TOKEN="$(cat ~/.avp-intervention-token)"
H="Authorization: Bearer $AVP_INTERVENTION_TOKEN"
EP=<episode_id>

curl -sH "$H" $AVP_INTERVENTION_URL/v1/intervention/episodes/$EP                          # 読む
curl -sH "$H" "$AVP_INTERVENTION_URL/v1/intervention/episodes/$EP/review-packet?format=markdown" > packet.md
curl -sH "$H" -H 'Content-Type: application/json' -X POST $AVP_INTERVENTION_URL/v1/intervention/episodes/$EP/commands \
  -d '{"command_id":"'"$(uuidgen)"'","type":"request_hold","payload":{"reason":"確認したい"}}'  # 止める
curl -sH "$H" $AVP_INTERVENTION_URL/v1/intervention/episodes/$EP/change-sets/<id>           # 差分・影響・費用
```

CLI（`scripts/intervene.py`）は同じ API を呼ぶだけの薄い道具（§6）。

## 6. CLI（`scripts/intervene.py`）

標準ライブラリ（`urllib`）だけで HTTP API を呼ぶ。DB・store には触れない（repo のコードを import しない）。
token は**環境変数 `AVP_INTERVENTION_TOKEN` だけ**から読む（引数に取らない = シェル履歴・`ps` に残さない）。
base URL は `--url` か `AVP_INTERVENTION_URL`（既定 `http://localhost:8000`）。localhost 以外へ平文 http で送るときは警告する。

```bash
scripts/intervene.py show EP                                   # Episode を読む
scripts/intervene.py decisions EP                              # 判断記録
scripts/intervene.py packet EP --format md -o packet.md        # ReviewPacket（json / md / html）
scripts/intervene.py comment EP --body "ここを短く" --segment-id s3   # コメント（--scene-id / --revision-id 等）
scripts/intervene.py hold EP --reason "確認したい"                # 停止の要求
scripts/intervene.py propose EP --file proposal.json           # 変更案（ProposeChangePayload の JSON）
scripts/intervene.py import EP --file proposals.json           # 変更案ファイル（ChangeProposalFile）の取り込み
scripts/intervene.py propose-storyboard --episode EP --file p.json   # 絵コンテ変更案（StoryboardPatchProposal。ADR-0043）
scripts/intervene.py change-sets EP                            # 変更案の一覧
scripts/intervene.py diff EP --change-set CS                   # 差分・影響範囲・見積り
scripts/intervene.py diff EP --a REV1 --b REV2                 # 2つの revision の比較
scripts/intervene.py apply EP --change-set CS --max-cost 0.50  # 適用（見積りがこれを超えたら適用しない）
scripts/intervene.py reject EP --change-set CS --reason "不要"
scripts/intervene.py resume EP [--start-stage voice]           # 再開
scripts/intervene.py release-checkpoint EP --point before_upload --manifest-id M
scripts/intervene.py status COMMAND_ID                         # command の状態（received ≠ applied）
scripts/intervene.py commands EP
scripts/intervene.py hash-token                                # $AVP_INTERVENTION_TOKEN の sha256（設定の key）
```

- command 系（comment / hold / propose / apply / reject / resume / release-checkpoint）は毎回 uuid4 の command_id を作り、
  **送る前に stderr へ `command_id=...` を出す**。接続が切れて結果が分からないとき（終了コード 3）は、表示された
  `--command-id` を付けて同じ内容で再送する（API は保存済みの状態を `200` で返し、二重にならない）。
- 楽観的な競合検出には `--expected-control-revision` / `--expected-target-revision`（読み取りで得た値）。
- 終了コード: `0` 成功（2xx）、`1` API がエラーを返した（応答の `detail` を表示）、`2` 使い方・token 未設定、`3` 応答なし。
- `set_mode` の CLI はない（人専用。必要なら所有者が curl で送る）。予算・Schedule・公開設定の CLI もない（INV-47）。

## 7. 担当 A / B との接続の状態

`apps/api/intervention_backend.py::load_real_modules` が唯一の接続点。A / B のモジュールは**直接の lazy import**
（初回使用時）で、組めなければ `ModulesUnavailableError`（`503 intervention_unavailable`、型名だけをログに残す）。
API 全体は落ちない。

- B `infrastructure.intervention.commands`: `receive_command(session, episode_id, envelope, principal)`
  （`ReceiveResult.command` / `.created`）、`Principal(actor=..., scopes=...)`、拒否は `CommandReceiveError`（`code`）。
  `code == "not_found"` は `404 episode_not_found`、他は上表。同じ id・違う内容で `store.DuplicateCommandError`、
  同じ id・同じ内容でも**保存済みの主体が違えば** `409 command_id_reused`。
- B の `InterventionCommandWorkflow` の入力は `{"command_id": ...}`、id は `intervention_command_workflow_id`、queue は
  `INTERVENTION_TASK_QUEUE`。worker 側の実装は `workers/intervention/`（B2）にある。
- start は **best-effort**: commit 後に 5 秒（`START_TIMEOUT_SECONDS`）で打ち切り、失敗・タイムアウトでも `202 received`
  を返す（未配送のまま残り、再配送 Schedule が拾う。[intervention.md](../domain/intervention.md) §4）。
  start に成功したら API が `mark_command_dispatched` する。Temporal client は 1 つを使い回すが、event loop が変わる・
  start が失敗したときは捨ててつなぎ直す。
- A `EpisodeReadModel(session_factory, artifact_store)` の `load(episode_id)`（Episode が無ければ `None` → 404）と
  `compare_revisions(a, b)`（`ValueError` → `422 revision_type_mismatch`）、`build_packet(view)` / `render_markdown(packet)`
  （同期・非同期どちらでもよい）。
- A `ChangeProposalFile` は `proposals`（`ProposeChangePayload` 互換）と任意の `episode_id` を持つ。

## 8. 判断の説明と Episode 横断の検索（ADR-0042、scope `read`）

対話 AI アシスタント向けの読み取り route。どちらも GET で、保存済みの行だけを読む（LLM を呼ばない・書き込まない）。
実装は `apps/api/routers/intervention_insights.py` と `infrastructure/intervention/explain.py`。

| route | 返すもの |
|---|---|
| `GET /v1/intervention/episodes/{episode_id}/decisions/{decision_id}/explanation` | 判断の問い・候補・選択・理由（`rationale.kind` は保存値のまま。`reported` は `provenance=self_reported_by_model`・`verified=false`）・根拠参照と `evidence_check`・役割／model／prompt_version（`agent_runs`）・入力 snapshot 参照・制約・留保・対象 revision に向けた注記と command・下流 revision（`revision_links`）。記録が無い項目は `null` で、名前を `not_recorded` に列挙する（token 使用量・費用が未計上なら `agent_run.token_usage` / `agent_run.estimated_cost_usd`）。Scene と Workflow への結び付き（`scene_id` / `workflow_id` / `workflow_run_id`、`agent_run` の `workflow_id` / `workflow_run_id` / `activity_id`。migration 0018）も同じで、記録が無ければ `null` と `not_recorded`（Activity の外で作った行・Scene を特定しない判断）。その Episode に無い判断は `404 decision_not_found` |
| `GET /v1/intervention/decisions?decision_type=&role=&episode_id=&scene_id=&limit=&before=` | Episode を跨いだ判断履歴（正本の PostgreSQL）。新しい順。`limit` は 1〜200（既定 50）、`before` は前ページの `next_before`（不正なら `422 invalid_cursor`）。`role` は `agent_runs` の役割、`scene_id` はその Scene を対象に記録した判断（各要素に `scene_id` / `workflow_id`） |
