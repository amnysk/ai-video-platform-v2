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
  `contracts.intervention.REQUIRED_SCOPE`（`comment`・`set_user_constraint` → comment、`request_hold`・`request_cancel`
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
| 401 | `unauthenticated` | token が無い・違う（`WWW-Authenticate: Bearer`） |
| 403 | `missing_scope`（`required_scope` 付き） | principal に必要な scope が無い |
| 404 | `episode_not_found` / `command_not_found` / `change_set_not_found` / `revision_not_found` | 対象が無い・その Episode のものでない |
| 409 | `command_id_reused` | 同じ `command_id` が**違う内容**で既に使われた |
| 422 | `unsupported_command` | command 型が語彙に無い（予算・Schedule・公開設定を含む全ての未知の型） |
| 422 | `invalid_payload`（`errors` 付き。入力値は返さない） | envelope・payload の形が違う／余分な項目 |
| 422 | `episode_mismatch` | 変更案ファイルが別の Episode のもの |

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
 "type": "comment | set_user_constraint | request_hold | request_cancel | propose_change | apply_change | reject_change | resume | release_checkpoint | set_mode",
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

## 6. 担当 A / B への依存（統合前の状態）

`apps/api/intervention_backend.py::load_real_modules` が唯一の接続点（遅延 import）。この branch の時点では
A / B のモジュールが無いので、次は **fake でしか動かしていない**（統合後に親が確認する）:

- B `infrastructure.intervention.commands.receive_command(session, episode_id, envelope, principal)` の戻り値は
  `command`（`store.Command`）と `created` を持つ（または `(command, created)` の tuple）。
  `Principal(actor=..., scopes=...)` のキーワード引数。同じ id・違う内容で `store.DuplicateCommandError` を送出。
- B の `InterventionCommandWorkflow` の入力は `{"command_id": ...}`、id は `intervention_command_workflow_id`、
  queue は `INTERVENTION_TASK_QUEUE`。start に成功したら API が `mark_command_dispatched` する。
- A `EpisodeReadModel(session_factory, artifact_store)` の `load(episode_id)` と `compare_revisions(a, b)`、
  `build_packet(view)` / `render_markdown(packet)`（同期・非同期どちらでもよい）。
- A `ChangeProposalFile` は `proposals`（`ProposeChangePayload` 互換）と任意の `episode_id` を持つ。
