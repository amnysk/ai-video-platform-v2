# Codex の model 設定（日次 7 日連続失敗の原因と対処）

状態: 調査完了・**本番設定は未変更**（所有者の承認待ち）。調査日 2026-10-21（読み取りのみ）。

## 症状と根本原因（証拠）

- `topic_generate_candidates`（script-worker）が 2026-10-14〜10-20 の 06:00 JST 実行で毎回失敗。
  worker ログの本文:
  `The '<model>' model is not supported when using Codex with a ChatGPT account.`（HTTP 400）
- 拒否された model（script-worker のログ集計）: **gpt-5.5**（10-14〜10-17）、**gpt-6-sol**（10-18〜10-20）。
  つまり 10-18 の `gpt-5.5 -> gpt-6-sol` への書き換えでは直っていない。
  10-07〜10-13 にも `script_generate` の失敗があり、model 問題の前から別の失敗が混ざっている（本書の範囲外）。
- model の出所は **コードではなく `~/.codex/config.toml`**。
  `CODEX_MODEL` は空（`compose.yaml` は `${CODEX_MODEL:-}` を渡すだけ）で、空のとき
  `infrastructure/providers/codex_cli.py` は `-m` を付けず codex の既定に従う。コードは model を固定しない。
- config.toml は**コピーではなく bind mount**（`${CODEX_HOME_HOST_DIR:-$HOME/.codex}` -> `/home/avp/.codex`, rw）。
  ホストとコンテナは同一ファイル（mtime 2026-10-18 22:15 JST = 13:15 UTC、先頭の `model = "gpt-6-sol"`）。
  ホストの対話用 codex の設定と共有しているので、**対話で model を変えると本番の日次が変わる**。
- 版: コンテナ codex-cli **0.154.0**（`Dockerfile` の `CODEX_VERSION`）、ホスト codex-cli **0.163.0**。
  `models_cache.json` はホストの 0.163.0 が書いた（mount 経由でコンテナにも見える）。

## 使える model（結論: UNKNOWN）

ローカルの `models_cache.json`（client_version 0.163.0）の一覧は次のとおり。

| slug | 備考 |
|---|---|
| gpt-6.1-sol | 最新（priority 1） |
| gpt-6-astra, gpt-6-sol, gpt-6-luna | gpt-6-sol は「Previous generation」 |
| gpt-5.6-sol, gpt-5.6-terra, gpt-5.6-luna | 旧世代 |
| gpt-reserve, codex-auto-review | hidden |

**この一覧は「使える」の証拠にならない。** gpt-6-sol は一覧にあるのにサーバーが 400 で拒否した。
一覧はカタログで、アカウント／プラン／CLI 版による利用可否を反映しない。
`config.toml` の `[notice.model_migrations]` は `gpt-5.5 -> gpt-5.6-sol`、`gpt-5.4 -> gpt-5.6-terra`
（CLI 自身が出した移行先）を示し、`[tui.model_availability_nux]` は gpt-5.5 / gpt-5.6-sol /
gpt-6-astra / gpt-6.1-sol を列挙するが、どちらも利用可否の保証ではない。

候補（根拠の強さ順、いずれも未検証）: `gpt-5.6-sol`（CLI が gpt-5.5 の移行先に指定）、`gpt-6.1-sol`。
gpt-6-sol が拒否された原因がアカウントか 0.154.0 という古い client かも切り分けられていない。

## 追加調査（2026-10-25、読み取りのみ・利用枠は使っていない）

- `codex debug models`（カタログ取得のみ、prompt なし）= `models_cache.json` と同じ 9 件。
  **gpt-6-sol は今もカタログにある**のに本番では 400。カタログは利用権の証拠にならない（再確認）。
- 同アカウントの**ホスト対話セッションの記録**（`~/.codex/state_5.sqlite` の `threads`、
  rollout の `task_complete`）: 10-18 22:13〜22:21（codex 0.163.0, VS Code）に **gpt-6-sol が 2 スレッドで
  完了**（計 8.2M tokens）、**gpt-5.6-terra が 1 スレッドで完了**（2.6M tokens）。gpt-5.5 は 0 tokens。
  => アカウントは gpt-6-sol / gpt-5.6-terra を使える。コンテナ（codex 0.154.0）だけが 400 になる。
  原因の第一候補は **client 版**（0.154.0 → 0.163.0 以上）。**未証明**（probe で切り分ける）。
- 推奨 model（脚本の品質順、**未実測**）: `gpt-6.1-sol`（カタログ上の最新・「GPT-6.1-Sol: near-Astra 性能を
  低コストで」）> `gpt-6-sol`（ホストで実績あり）> `gpt-5.6-terra`（ホストで実績あり）。
  `gpt-6-astra` は最上位だが利用枠の消費が大きい（日次の台本に必要な水準を超える）。**どれも設定しない**:
  probe を所有者が承認してから、通った最上位を固定する。

### 所有者承認が要る probe（利用枠を 1 回ずつ使う。承認前は実行しない）

判定したいのは**本番のコンテナの codex（0.154.0）**で通るかなので、まずコンテナで行う
（`docker exec` は実行中の worker に対する読み取り的な操作で、再作成はしない）:

```bash
# 1) コンテナ版 0.154.0 で、推奨順に 1 回ずつ（通ったら止める。read-only sandbox、一時 session）
for m in gpt-6.1-sol gpt-6-sol gpt-5.6-terra; do
  echo "== $m"; docker exec avp2-script-worker-1 \
    codex exec --ephemeral -s read-only -m "$m" "reply with the single word: ok" 2>&1 | tail -3
done
# 2) 全部 400 なら、client 版の切り分け: ホスト 0.163.0 で同じ 1 回（gpt-6-sol は対話で実績がある）
codex exec --ephemeral -s read-only -m gpt-6-sol "reply with the single word: ok"
```

結果の読み方: コンテナで通った最上位 = 固定する model。コンテナは全部 400・ホストは通る = 版ずれ
（`Dockerfile` の `CODEX_VERSION` を上げる別 PR が先）。両方 400 = 認証・アカウント（再ログインが先）。

### 設定変更（probe の後。所有者が実施。共有の config.toml には触れない）

`.env`（リポジトリ直下、本番ホスト）に model を**明示**する。config.toml は対話の codex と共有なので
本番の model の出所にしない:

```
CODEX_MODEL=<probe で通った model>
CODEX_ALLOWED_MODELS=<probe で通った model>
CODEX_REQUIRE_PINNED_MODEL=true
```

反映: `docker compose up -d script-worker storyboard-worker scene-alternative-worker`（再作成）。
3 つの worker は compose の `x-codex-env` から同じ値を読む。確認は読み取りだけで足りる:
`python scripts/codex-model-preflight.py`（利用枠を使わない）。日次の入口（ADR-0058）は、
許可リスト外の model / 未固定を Codex を呼ぶ前に止める。

## 提案する変更（承認後に実施）

1. （先に実測）承認のもと、ホストで 1 回だけ最小の実呼び出しで判定する:
   `codex exec --ephemeral -s read-only -m gpt-5.6-sol "reply: ok"`（必要なら gpt-6.1-sol も 1 回）。
   これが ChatGPT アカウントで通れば、その model が使える証拠になる。コンテナ版（0.154.0）でも同じ 1 回を
   `docker exec avp2-script-worker-1 codex exec ...` で行うと client 版の影響も分かる。
2. 設定変更は、ホストの対話設定を巻き込まない `.env` 側を推奨:
   - ファイル: リポジトリ直下 `.env`（本番ホスト）
   - キー: `CODEX_MODEL`、旧値 `（空）` -> 新値 `gpt-5.6-sol`（1 で実測できた model）
   - 同時に `CODEX_ALLOWED_MODELS=gpt-5.6-sol`（実測できた集合）を入れる
   - 反映: `docker compose up -d script-worker storyboard-worker scene-alternative-worker`（再作成が要る）
   代替: `~/.codex/config.toml` 先頭の `model = "gpt-6-sol"` を実測できた値に戻す
   （対話の codex にも影響する）。
3. 版ずれが原因と分かった場合のみ、`Dockerfile` の `CODEX_VERSION` をホストに合わせる（別 PR）。

## ロールバック

- `.env` の `CODEX_MODEL` を空に戻して worker を再作成（config.toml の既定に戻る）。
- config.toml を触った場合は変更前の 1 行（`model = "gpt-6-sol"`）に戻す。
- 失敗時は何も壊れない: 台本生成は失敗するだけで、予約・課金（有料 provider）には届いていない。

## 検証手順

1. `docker logs avp2-script-worker-1` の起動行で `codex model ... in the allowlist`（allowlist 設定時）。
2. 実測した model で単発の生成が通ること（上の 1）。
3. 承認のもと日次を手動で 1 回起動し、`topic_generate_candidates` が成功すること。

## 今回入れたガード（コード）

- `contracts/codex_models.py`: `CODEX_ALLOWED_MODELS` の語彙と拒否メッセージの解析（唯一の宣言元）。
  **既定は空 = 未検証**。コードは「この model は使える」と主張しない。
- worker 起動時（script / storyboard / scene-alternative）に `verify_codex_model` が実効 model
  （`CODEX_MODEL` > config.toml のトップレベル `model`）を許可リストと照合する。
  許可リストが空なら警告（`unverified`）、リスト外なら `UnsupportedModelError` で起動を止める。
- 実行時に 400 を受けたら `UnsupportedModelError(model=...)`（`ProviderUnavailableError` の子、
  `needs_input`・retry しない）。メッセージに model 名と修正先を含み、secret は含めない。
- 注意: 許可リストを設定した worker は、リスト外 model のままだと**起動に失敗する**（意図した fail-fast）。

## 所有者の承認が要るもの

- 本番設定の変更（`.env` の `CODEX_MODEL` / `CODEX_ALLOWED_MODELS`、または config.toml）と worker の再作成
- 実 model の呼び出し 1〜2 回（判定用。課金枠・アカウント利用に当たる）
- 日次生成の再開（失敗した 7 日分の補填をするかも含む）
