# ADR-0042: 対話 AI アシスタントは介入 API の tool adapter として接続する

## Status

Proposed（2026-10-15）。設計 JP-MAS-001 v2.0 §8「対話するAIアシスタント向け」・§13「UI／ChatGPT接続」の
初期実装。前提は [ADR-0041](./0041-decision-records-and-intervention.md) と
[docs/domain/intervention.md](../domain/intervention.md)。

## Context

実査（2026-10-15、`claude/jpmas-integration`）:

- 設計 §8 の操作（read_episode / read_decisions / compare_revisions / export_review_packet / propose_change /
  request_hold / get_command_status / apply_change / resume_episode）は全て `/v1/intervention` の route と
  `scripts/intervene.py` に存在する。AI アシスタントが関数呼び出し（tool use）で使える定義・実行器は無い。
- 介入 API は principal（token の sha256 → actor_type・scopes）で認可し、command 型ごとの必要 scope は
  `contracts.intervention.REQUIRED_SCOPE` が唯一の宣言元。AI principal は人専用 scope（`set_mode`）を得られず
  （INV-47）、AI による有料の ChangeSet 適用は拒否される（`test_ai_may_apply_only_zero_cost_changes`）。
- 同じ `command_id` の再送は1つの論理変更にしかならない（INV-42）。
- 判断の「説明」を返す route と、Episode を跨いだ判断履歴の検索は無い（過去の制作結果を参照した提案に必要）。
- 設計 §8: 「このChatGPTが将来自動的に本番へ接続できるとは仮定しない」「将来の接続は同じAPIのadapterとして追加する」
  「MCP等を使う場合も、DBへの直接書き込みより既存の検証を通る介入APIを使う」。

## Decision

**AI アシスタントは `apps/assistant/`（tool adapter）を通じて介入 HTTP API だけを呼ぶ。対話する LLM 自体は repo の外
（Claude・ChatGPT 等のクライアント）に置き、repo は tool の定義・実行器・説明用の読み取り route を持つ。**

1. **境界**: `apps/assistant/` は `apps/web` と同じ位置づけ（INV-1 の趣旨）。呼んでよいのは `/v1/intervention` の
   HTTP と `contracts` だけ。`infrastructure`・`workers`・`domain` の永続化・`temporalio`・`sqlalchemy`・`psycopg` を
   import しない。新しい不変条件 **INV-48** とし、`tests/architecture/` で機械検査する。
2. **tool の唯一の宣言元**: `contracts/assistant_tools.py`。各 tool は名前・説明・入力 JSON Schema・効果の種別
   （`read` / `annotate` / `propose` / `control` / `apply`）・必要 scope を持つ。command を送る tool の必要 scope は
   `REQUIRED_SCOPE` から導き、literal で書き直さない（AGENTS.md §8）。予算上限・Schedule・公開設定を変える tool は
   定義しない（INV-47 と同じ）。
3. **提案と適用の分離**:
   - `propose_*` は ChangeSet を作るだけで、生成・課金を始めない（設計 §9「コメントと適用命令は別」）。
   - `preview_change_impact` は ChangeSet の影響範囲（stale・再生成・再利用）と見積り費用を返す。未見積りは
     `null` のまま返し、0 と表示しない。
   - `apply_change` は `change_set_id` と、利用者に提示した `acknowledged_estimated_cost_usd` を必須にする。
     adapter は送信直前に現在の見積りを読み直し、一致しなければ送らずに `cost_changed` を返す（提示した費用と
     違う変更を適用しない）。権限の正本はサーバ（scope と AI の有料適用拒否）で、adapter の検査は補助。
   - サーバが権限不足・有料のため拒否した場合、adapter は `needs_human_approval` として、人が実行する手順
     （`scripts/intervene.py apply …`）と影響・見積りを返す。AI が権限を迂回する経路は作らない。
4. **冪等**: command を送る tool は `command_id = uuid5(ASSISTANT_NAMESPACE, "{session_id}:{tool_call_id}")` を使う。
   同じ tool 呼び出しの再試行は INV-42 により1つの論理変更にしかならない。**INV-49** とし、テストで検査する。
5. **説明と検索（読み取り route を追加）**:
   - `GET /v1/intervention/episodes/{episode_id}/decisions/{decision_id}/explanation`: 判断の問い・候補・選択・
     理由（`rationale_kind` を明示し、`reported` を検証済みの事実として扱わない）・根拠参照と照合結果
     （`evidence_check`）・役割と model 識別子・prompt_version・入力 snapshot 参照・制約・留保・その判断の対象への
     人／AI の介入・下流 revision（`revision_links`）を、**保存済みの記録だけから決定的に**組み立てる。LLM を呼ばない。
     記録の無い項目は欠測として返し、補完しない（設計 §7）。
   - `GET /v1/intervention/decisions?decision_type=&role=&episode_id=&limit=&before=`: Episode を跨いだ判断履歴
     （正本の PostgreSQL から）。OpenSearch は使わない（検索障害で正本の読み取りを失わない / 設計 §2）。
   - どちらも scope `read`。INV-16（重い処理をしない）を守る。
6. **記録と相関**: adapter は全ての要求に `X-Correlation-ID: assistant:{session_id}:{tool_call_id}` と
   `X-Assistant-Tool: {tool_name}` を付ける。API は `/v1/intervention` の全要求について構造化ログ
   `intervention.api.request`（route の template・principal の actor_type／actor_id・status・duration_ms・
   correlation_id・tool_name・episode_id）を出し、既存の OpenSearch 投影経路に乗せる。token・本文は出さない（INV-39）。
   正本は従来どおり command 行・`episode_events`。
7. **入口**: `scripts/assistant.py tools`（tool 定義を JSON で出す。tool use の定義としてそのまま渡せる形）と
   `scripts/assistant.py call <tool> --input <json> --session <id> --tool-call-id <id>`（実行）。MCP server は
   新しい依存（`mcp` パッケージ）と常駐プロセスを足すため今回は作らない。追加するときは別 ADR で、同じ
   `contracts/assistant_tools.py` と実行器を包むだけにする。

## Alternatives

- **repo 内に LLM の対話ループを持つ**: 新しい有料 API（LLM provider）・秘密・費用の計上が必要になり、設計 §8 の
  「自動的に本番へ接続できるとは仮定しない」に反する。採らない。
- **adapter が DB を直接読む**: 権限・期待版・scope の記録（INV-47）を迂回する。設計 §8 が明示的に否定。採らない。
- **説明文を LLM に生成させる**: 記録に無い理由を作り得る（設計 §7「後から架空に埋めない」）。説明は記録から
  決定的に組み立て、自然文への言い換えは利用者側の LLM に任せる（その場合も根拠は構造化の返り値）。採らない。
- **Episode 横断検索を OpenSearch で行う**: 副本で欠測・遅延があり得る。初期は PostgreSQL。将来の全文検索は別 ADR。

## Consequences

良い:
- AI アシスタントは人と同じ API・同じ検査・同じ記録を通る。提案は ChangeSet、適用は権限のある command だけ。
- 同じ tool 呼び出しの再試行で二重適用・二重課金が起きない。
- 説明は保存済み記録と照合可能で、自己申告の理由と検証済みの根拠を区別して返す。

悪い・引き受けた負債:
- 対話そのもの（会話履歴・自然文の要約）は repo の外にあり、会話の全文は保存しない。残るのは tool 呼び出しの
  相関 ID・command・ChangeSet・コメント。
- MCP 等の常駐 adapter は未実装。
- agent_runs の token usage・LLM 費用は未計上のまま（ADR-0041 の負債を引き継ぐ）。説明では欠測として示す。
