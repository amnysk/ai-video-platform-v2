# API の認証（ADR-0080 §B / INV-124・125）

変更系ルート（`POST` / `PUT` / `PATCH` / `DELETE`。`/episodes` 系と `/research` 系すべて）は
`Authorization: Bearer <token>` を要求する。読み取り（`GET /healthz`、`GET /episodes/{id}` など）は開けたまま。

- トークン未設定・ファイルが読めない・壊れている・他者が読める権限（0600 以外）なら、**変更系は全て 503**（無認証に倒れない）。
- トークンが無い・違う場合は 401。応答にもログにもトークンは出ない。ログには actor（`owner` / `agent`）が残る。
- 手動投稿（`POST /episodes/{id}/upload`）は、`AVP_QUALITY_GATE_ENFORCE` が ON のとき BLOCK を迂回できず、
  REVIEW を承認できるのは `owner` トークンだけ（`agent` トークンは 409 `quality: review_requires_owner`）。
  flag が OFF の間は従来どおり品質ゲートを通らない（ADR-0076）。

## デプロイ時の設定（本番は compose project `avp2`。この手順は所有者の判断で行う）

1. トークンを作る（repo の外・0600）:

   ```bash
   install -d -m 700 ~/.config/avp2
   umask 077
   {
     echo "owner:$(head -c 32 /dev/urandom | od -An -tx1 | tr -d ' \n')"
     echo "agent:$(head -c 32 /dev/urandom | od -An -tx1 | tr -d ' \n')"
   } > ~/.config/avp2/api-tokens
   ```

2. api コンテナへ読み取り専用で渡す（compose の override。`compose.yaml` のポート設定は変えない）:

   ```yaml
   services:
     api:
       environment:
         AVP_API_TOKENS_FILE: /run/avp-secrets/api-tokens
       volumes:
         - type: bind
           source: ${HOME}/.config/avp2/api-tokens
           target: /run/avp-secrets/api-tokens
           read_only: true
   ```

   ファイルの所有者はコンテナの実行 UID（`AVP_UID`）にする（0600 のため、別 UID では読めず 503 のままになる）。
3. 運用者の端末では `export AVP_API_TOKEN=<owner の token>`（`scripts/smoke*.sh`・`scripts/provider-credits.py resume --api-url`
   が読む）。エージェントには `agent` のほうを渡す。
4. 確認: トークン無しの `POST` が 401（設定済み）/ 503（未設定）、`GET /healthz` が 200。

## 呼び出し元の棚卸し（認証なしで API を呼んでいたもの。この枝で更新済み）

| 呼び出し元 | 変更 |
|---|---|
| `scripts/smoke.sh` / `smoke-script.sh` / `smoke-storyboard.sh` / `smoke-production.sh` / `smoke-render.sh` / `smoke-upload.sh` | `-H "Authorization: Bearer ${AVP_API_TOKEN:?…}"`（未設定なら実行前に止まる） |
| `scripts/provider-credits.py resume --api-url`（`ApiResumeClient`） | env `AVP_API_TOKEN` を bearer として送る |
| `docs/operations/{upload,storyboard,render}-worker.md` の curl 例 | ヘッダを追加 |
| Temporal の日次 Schedule・watchdog | API を経由しない（影響なし） |
| 本番 compose の healthcheck | `api` にはなし。`GET` なので影響なし |

## 後続の推奨（この枝では実施しない）

- `compose.yaml` の `api` の `ports: ["8000:8000"]` を `127.0.0.1:8000:8000` にする（LAN 192.168.1.9 に公開する理由が無い）。
- 認証できない `GET` の応答にも内部 ID が含まれる。公開を狭めるのは bind の変更で行う。
