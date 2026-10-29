# API の認証（ADR-0080 §B / INV-124・125）

変更系ルート（`POST` / `PUT` / `PATCH` / `DELETE`。`/episodes` 系と `/research` 系すべて）は
`Authorization: Bearer <token>` を要求する。読み取り（`GET /healthz`、`GET /episodes/{id}` など）は開けたまま。

- トークン未設定・ファイルが読めない・壊れている・他者が読める権限（0600 以外）なら、**変更系は全て 503**（無認証に倒れない）。
  読むのは変更系のリクエストのときだけ（GET はファイルに触れず、ファイルが壊れていてもログを出さない）。
- トークンが無い・違う場合は 401。応答にもログにもトークンは出ない。ログには actor（`owner` / `agent`）が残る。
- 手動投稿（`POST /episodes/{id}/upload`）は、`AVP_QUALITY_GATE_ENFORCE` が ON のとき BLOCK を迂回できず、
  REVIEW を承認できるのは `owner` トークンだけ（`agent` トークンは 409 `quality: review_requires_owner`）。
  flag が OFF の間は従来どおり品質ゲートを通らない（ADR-0076）。

## owner と agent のトークンが保証すること・しないこと（正直な記述）

- 保証: 変更系は token が無ければ通らない（LAN の第三者・設定ミスに対する門）。どちらの token で通ったかは actor として
  ログに残る（誰の意図で行った操作かの**記録**）。品質ゲート ON の REVIEW 承認は owner token の要求に結び付く。
- **保証しない**: owner と agent を同じ 0600 ファイルに置き、エージェントが動く OS ユーザーがそのファイルを読めるなら、
  エージェントは owner token を使える。この場合の owner / agent の区別は**帰属・意図の記録であって、セキュリティ境界ではない**。
  コンテナが `AVP_UID`（既定 1000）で動き、エージェントも同じ UID なら、ファイルの所有者を変えても分離にならない。
- 実効のある分離にする選択肢（任意）: owner token を**別ファイル**（`AVP_API_OWNER_TOKEN_PATH`）にし、エージェントの
  OS ユーザーが読めない場所・権限（owner 本人の所有、0600、親ディレクトリも 0700）に置く。コンテナがそのファイルを
  読めるよう、api コンテナだけ別 UID（agent のユーザーと異なる UID）で動かし、ファイルをその UID の所有にする。
  設定すると、共有ファイルの `owner:` 行は拒否され（agent が読めるファイルに owner token を残させない）、owner ファイルが
  設定されているのに読めないときは変更系が全拒否になる。
- **残るリスク**: (1) 上の分離をしない運用では、エージェントによる owner token の利用は防げない。(2) 分離しても、root・
  docker グループのユーザーは読める。(3) API 経由でない面（Temporal 7233・UI 8233・Postgres・MinIO が `0.0.0.0`）は
  この認証の外（監査 §4）。(4) token の失効・ローテーションの自動化は無い（ファイルを差し替える。再起動不要）。

## デプロイ時の設定（本番は compose project `avp2`。この手順は所有者の判断で行う）

1. トークンを作る（repo の外・0600。**ファイルの所有者はコンテナの実行 UID `AVP_UID`**。違うと 0600 のため API が読めず 503）:

   ```bash
   install -d -m 700 ~/.config/avp2/api
   umask 077
   {
     echo "owner:$(head -c 32 /dev/urandom | od -An -tx1 | tr -d ' \n')"
     echo "agent:$(head -c 32 /dev/urandom | od -An -tx1 | tr -d ' \n')"
   } > ~/.config/avp2/api/tokens
   ```

2. 渡し方は `compose.yaml` の `api` に組み込み済み: `${AVP_API_SECRETS_DIR:-$HOME/.config/avp2/api}` を
   `/run/avp-secrets/api` に読み取り専用で bind し、`AVP_API_TOKENS_PATH=/run/avp-secrets/api/tokens` を渡す。
   ディレクトリやファイルが無くても compose は起動し、API は変更系を 503 にする。
   **token は環境変数では渡せない**（ファイルだけ。`docker inspect` に token が出ないようにするため）。
3. （任意）owner を別ファイルにする: `deploy/api-owner-token.compose.yaml` を重ね、`.env` に
   `AVP_API_OWNER_TOKEN_PATH=/run/avp-secrets/api-owner/owner-token` を書く。owner ファイルは
   `${AVP_API_OWNER_SECRETS_DIR:-$HOME/.config/avp2/api-owner}/owner-token`（`owner:<token>` の 1 行、0600）。
4. deploy 前に `scripts/release-preflight.py` の `api_token_file` が PASS であること（runbook §1 の 3b）。
5. 運用者の端末では `export AVP_API_TOKEN=<owner の token>`（`scripts/smoke*.sh`・`scripts/provider-credits.py resume --api-url`
   が読む）。エージェントには `agent` のほうを渡す。
6. 確認: トークン無しの `POST` が 401（設定済み）/ 503（未設定）、`GET /healthz` が 200。

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
