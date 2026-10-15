# 対話 AI アシスタントの接続（tool adapter）

決定の理由は [ADR-0042](../decisions/0042-assistant-tool-adapter.md)、不変条件は INV-48（境界）と INV-49（冪等・費用確認）。
設計は JP-MAS-001 v2.0 §8。対話する LLM 自体は repo の外（Claude・ChatGPT 等のクライアント）に置き、repo が持つのは
**tool の定義・実行器・説明用の読み取り route**。会話の全文は保存しない（残るのは tool 呼び出しの相関 ID・command・
ChangeSet・コメント）。

## 1. 設定（所有者が行う）

アシスタント用の principal を `INTERVENTION_PRINCIPALS`（[intervention-api.md](./intervention-api.md) §1）に足す。
`actor_type` は `ai_assistant`。scope は必要な分だけ（`set_mode` は人専用で AI には付けられない）:

```json
"<sha256 of the assistant's token>": {"actor_type": "ai_assistant", "actor_id": "chat-assistant",
                                      "scopes": ["read", "comment", "propose", "hold"]}
```

- `read` だけなら読み取りの tool だけが動く。`comment` で `add_comment`、`propose` で `propose_*`、`hold` で
  `request_hold`、`resume` で `resume_episode`、`apply` で `apply_change`。
- **有料の適用を AI に任せない既定**: `apply` を付けなくてよい。付けても、AI（`ai_assistant`）は 0 円・ユーザー制約に
  触れない変更しか適用できない（サーバが拒否する。見積りが未計算の変更も拒否）。
- token は環境変数だけで渡す（シェル履歴・ファイル・ログに残さない）:

```bash
export AVP_INTERVENTION_URL=http://localhost:8000     # 省略時はこの値（scripts/intervene.py と同じ）
export AVP_INTERVENTION_TOKEN="$(cat ~/.avp-intervention-token)"
```

## 2. tool 定義を取り出す

```bash
python scripts/assistant.py tools               # name / description / input_schema / effect / required_scope
python scripts/assistant.py tools --tool-use    # name / description / input_schema だけ（LLM の tool use にそのまま渡せる）
```

| tool | effect | 必要 scope | 内容 |
|---|---|---|---|
| `get_episode_status` | read | read | Episode の状態・制御・採用 revision・費用・command |
| `read_decisions` | read | read | その Episode の判断記録 |
| `explain_decision` | read | read | 判断の説明（保存済みの記録だけから決定的に。欠測は `not_recorded`） |
| `search_decisions` | read | read | Episode を跨いだ判断履歴（新しい順・`before` でページング） |
| `compare_revisions` | read | read | 2つの revision の差分 |
| `export_review_packet` | read | read | ReviewPacket（json / markdown） |
| `list_change_sets` | read | read | 変更案の一覧（見積りの `null` は未見積り） |
| `preview_change_impact` | read | read | 変更案の差分・影響範囲・見積り費用 |
| `get_command_status` | read | read | command の状態（`applied` 以外は未適用） |
| `add_comment` | annotate | comment | アンカー付きコメント（segment_id / scene_id / asset_id / time_range / manifest_id / revision_id） |
| `propose_script_edit` | propose | propose | 台本の修正案（`script.edit_scenes`）。ChangeSet を作るだけ |
| `propose_scene_visual_override` | propose | propose | シーンの映像の修正案（`scene.override_visual`）。ChangeSet を作るだけ |
| `request_hold` | control | hold | 停止の依頼（送信済みの provider job は完了・課金されうる） |
| `resume_episode` | control | resume | 再開 |
| `apply_change` | apply | apply | ChangeSet の適用（§4） |

予算上限・Schedule・公開設定を変える tool は無い（INV-47）。必要 scope は `contracts.intervention.REQUIRED_SCOPE` から導く。
台本の映像文（`visual`）の修正は受け付けない（画像が変わらないため。映像は `propose_scene_visual_override`）。

### 楽観的な競合検出（直近に読んだ版を送る）

`get_episode_status` の結果は `control_revision` と `hint` を最上位に持つ。アシスタントは `request_hold` /
`resume_episode` / `apply_change` に、**直近に読んだ** `control_revision` を `expected_control_revision` として付ける
（`propose_*` は読んだ revision を `expected_target_revision` に）。Episode がその後に変わっていれば command は
適用されず `conflict` になる。省略すると検査されない。

## 3. tool を呼ぶ

```bash
python scripts/assistant.py call get_episode_status --input '{"episode_id": "<uuid>"}' \
    --session chat-2026-10-15 --tool-call-id call-0001
echo '{"episode_id": "<uuid>", "anchor": {"segment_id": "s3"}, "body": "ここを短く"}' | \
    python scripts/assistant.py call add_comment --input - --session chat-2026-10-15 --tool-call-id call-0002
```

- 結果は JSON（`status` を持つ）。終了コード: `0` ok / submitted、`1` サーバの拒否・要確認（`cost_changed`・
  `needs_human_approval`・`missing_scope`・`error` 等）、`2` 使い方・入力不正・token 未設定、`3` 応答なし。
- **`--tool-call-id` が冪等キー**: `command_id = uuid5(ASSISTANT_NAMESPACE, "{session}:{tool_call_id}")`。応答が分からない
  とき（終了コード 3）は**同じ `--session` / `--tool-call-id` で再試行**する。API は保存済みの command を返し、
  二重の提案・適用・課金にならない（`replayed: true`）。別の入力に同じ `tool-call-id` を使うと `command_id_reused`。
- command を送る tool の結果の `applied` が `true` になるのは `command_status=applied` のときだけ。
  `received` / `validated` / `applying` は**未適用**。`get_command_status` で確認する。
- 全ての要求に `X-Correlation-ID: assistant:{session}:{tool_call_id}` と `X-Assistant-Tool: {tool}` が付く。
  OpenSearch では `event_name:intervention.api.request` を `correlation_id` / `tool_name` / `episode_id` で引く
  （token・Authorization・本文は記録しない）。正本は `intervention_commands` と `episode_events`。

## 4. 有料の変更を適用する流れ（承認）

1. `propose_*` → command が `applied` になると ChangeSet（`result.change_set_id`）ができる。**提案は生成・課金を始めない**。
2. `preview_change_impact` で影響範囲と見積り費用（USD。`null` = 未見積り）を取り、**利用者に提示する**。
3. `apply_change` に `change_set_id` と、提示した `acknowledged_estimated_cost_usd`（未見積りなら明示的な `null`）を渡す。
   adapter は送信直前に変更案を読み直し、見積りが違えば**何も送らず** `cost_changed` を返す（現在の見積りと影響つき。
   利用者に再提示して呼び直す）。
4. サーバが権限不足（`403 missing_scope`）、または AI による有料適用として拒否したときは
   `status: needs_human_approval`（`impact`・`estimated_cost_usd`・`human_command`）を返す。**AI は再試行せず**、
   人が `human_command` を実行する:

   ```bash
   python scripts/intervene.py apply <episode_id> --change-set <change_set_id> --max-cost <見積り>
   ```

   サーバの拒否理由は**そのまま**返る（`reason` = サーバの `detail` コード、なければ `forbidden`。`server_reason` /
   `server_detail` も付く）。`human_command` は、保存済みの command 由来の id が UUID の正規形のときだけ、
   `shlex.quote` を通して作る（通らなければ付けず、人が手で確認する）。`resume_episode` も、サーバが AI による
   有料の再生成を伴う再開を拒否したとき（`detail=ai_resume_requires_paid_generation`）は `needs_human_approval`
   と `python scripts/intervene.py resume <episode_id>` を返す（再開は有料の再生成を始めうる）。
   同じ `tool_call_id` が別の command（種類・ChangeSet・Episode が違う）に使われていたら、適用結果として返さず
   `command_id_reused`。

   人が適用すると、ChangeSet の `actor_type=ai_assistant`、適用 command の `actor_type=human` として両方が残る。

## 5. 判断の説明と履歴

`explain_decision` / `search_decisions` は新しい読み取り route（[intervention-api.md](./intervention-api.md) §8）を呼ぶ。
説明は保存済みの記録だけから組み立て、LLM を呼ばない。`rationale.kind=reported` はモデルの自己申告（`verified=false`）で、
検証済みの事実として扱わない。token 使用量・費用は ADR-0041 の負債で未計上のため `agent_run.token_usage` /
`agent_run.estimated_cost_usd` は `null`（`not_recorded` に列挙）。自然文への言い換えは利用者側の LLM が行う。

## 6. 未実装

MCP server は作っていない（新しい依存と常駐プロセスを足すため。追加するときは別 ADR で、同じ
`contracts/assistant_tools.py` と `apps/assistant/tools.py` の実行器を包むだけにする）。
