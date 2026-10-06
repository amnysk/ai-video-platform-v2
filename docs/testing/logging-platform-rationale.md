# ログ基盤（収集・検索側）のテストが存在する理由

対象: ADR-0040 の `deploy/logging/`・`scripts/gen_log_mapping.py`・アプリ compose の `logging:`。
実測の根拠は ADR-0040 と [platform.md](../observability/platform.md)（OpenSearch 3.8.0 /
Fluent Bit 5.1.2、2026-09-30）。

## 1. `tests/contract/test_log_contract_mapping.py`

- **生成物が最新であること**: mapping と Collector の型表は `contracts/log_contract.py` から生成する。
  フィールドを足して再生成を忘れると、新しいフィールドは `dynamic: false` で黙って検索できなくなり、
  Collector は未知のキーとして `attributes.collector_moved` へ退避する。どちらもエラーにならないので、
  検査しなければ気付けない（AGENTS.md §7 の「片側だけの更新」）。
- **写像規則（型ごと）**: boolean・keyword・text に `ignore_malformed` を付けると index template の登録が
  400 で拒否される（実測）。逆に数値・日付に付け忘れると、型の合わない1件で bulk item が 400 になり
  Fluent Bit が上限まで再送して破棄する。`@timestamp` に付けると値の無い文書が Dashboards の時間軸から
  消えるので、付けないことも固定する。
- **infra の mapping は app の部分集合で、型が同じ**: 両系統を1つの index pattern 群で検索したときに
  同名フィールドの型が食い違うと Dashboards が conflict として扱う。
- **Lua の型表と label 定数**: Collector の振り分け（`avp.logging=app`）と型修復は Lua の表を読む。
  表と契約がずれると、アプリの行が infra 系統へ流れる・正しい値が退避される。
- **infra の mapping は契約の `INFRA_FIELD_NAMES` と一致し、生成器はその集合を自前で持たない**（I-7）:
  集合が生成器にあると、契約を読む側（Collector の Lua・検索側）と別の場所に同じ真実が散る（AGENTS.md §8）。
- **`--check` の終了コード**: CI や手元で drift を検出する手段として使えることを固定する。

## 2. `tests/contract/test_logging_platform_config.py`

設定ファイルだけを読み、ADR-0040 §4〜§7 の決定が黙って外れることを止める。どれも「外れても起動はする」
（＝運用で気付かない）ものだけを選んでいる。

- **digest 固定と VERSIONS.md の一致、OpenSearch と Dashboards の同版**: tag だけにすると知らないうちに
  別の版（`id_key` や bulk 応答の扱いが違う）になる。Dashboards は版の違う cluster を拒否する。
- **loopback だけの公開、docker.sock 無し、Fluent Bit の read-only・cap_drop・internal network・
  containers/ だけの ro mount・コンテナ内 root**: Collector は全サービスの秘密（config.v2.json）を読める
  権限で動く（実測）。その囲いが緩むと漏れ先ができる。uid を 1000 に戻すと無音で何も読めなくなる。
- **秘密を env で渡さない・admin 証明書は setup の one-shot だけ・repo に鍵や hash が無い**: env は
  `docker inspect` で見える。
- **秘密はファイル単位で mount し、サービスごとの許可リストに収まる**（I-9）: ディレクトリごと渡すと
  bootstrap・securityadmin にも CA の秘密鍵（`ca.key`、証明書を発行できる）や node 鍵が見える。`ca.key` は
  どのコンテナにも渡さない。
- **資源の上限（mem=memswap、heap、oom_score_adj、Dashboards は profile）**: 本番ホストは swap が満杯で、
  ログ基盤が本番 worker より先に落ちる前提で共存を認めている（ADR-0040 §7）。
- **sentinel と volume-guard**: project 名の違いで空の volume が作られると、OpenSearch は空で起動し、
  Fluent Bit は位置 DB 無し＋`read_from_head=true` で既存の大きなログを全量読む。
- **tail / output の値**: `*-json.log*`・`read_from_head`・`buffer_max_size`/`skip_long_lines`・
  `create`+`id_key`・`suppress_type_name`・`tls.verify_hostname`（既定 off）・`buffer_size 4M`・
  有限 retry・`trace_error off`。どれも実測した失敗（停止中 rotation の欠損、32k 超の行で監視停止、
  `_type` の 400、応答の溢れによる全体再送、値のプレビューの露出）への対策で、既定値に戻ると黙って失敗する。
- **OpenSearch の設定・ロール・ISM・bootstrap の順序・saved objects**: auto_create の値、DN の順序
  （逆だと admin 証明書が 401）、writer に検索・削除・作成が無いこと、demo ユーザーを持ち込まないこと、
  `server_username`（無いと Dashboards が 403 で起動しない）、保持の閾値、bootstrap が書く前に alias 誤作成を
  検出すること、saved search が存在する index pattern を参照すること。
- **スクリプトがパスワードを curl の argv に出さない**（I-6）: `-u user:pass` はホストの `ps` で誰にでも
  見える。0600 の一時 config を `-K` で渡す。
- **check-pipeline が系統別に lag を見て、environment の食い違いを数える**（I-5/I-6）: 1つの lag では
  temporal 等の infra の行が絶えず入るので app の収集停止が隠れる。compose の既定 `dev` が本番の行を
  `avp-app-prod-*` に `environment=dev` で入れた事故（I-5）を、設定の取り違えとして検出する。
- **Collector の行の上限 `COLLECTOR_LINE_MAX_BYTES` が tail の `buffer_max_size` と同じ値で、Lua がそれを使う**
  （I-15）: `buffer_max_size` は Docker の partial 1つずつにしか効かず、結合後の行は無制限に filter へ届く
  （実測）。上限を契約の1箇所に置き、設定と Lua の両方がそれに一致することを固定する。
- **安全化の Lua に O(n²) だった形（`%a[%w%+%.%-]*://`、`%[parameters: .-%]`）が戻らない**（I-15）:
  これらは長い行で Fluent Bit 全体を無言で止めた。実際に線形であることは integration test（§3）が
  時間で確かめるが、submit 前の検査（unit/contract）でも同じ形の再導入を止める。
- **試験用 ISM は override からだけ使われる**: 分単位で削除する policy が本番に入ると検索用ログが数分で消える。

## 3. `tests/integration/test_logging_collector_lua.py`

本物の `fluent-bit.yaml` の filter 列を採用版イメージ（5.1.2）で通す。Lua は手元にインタプリタが無く、
Fluent Bit の Lua 実装（LuaJIT）と msgpack 変換の癖（配列と map の区別、null の扱い）は実物でしか
確かめられないため、unit test にしない。

- **compose project の完全一致**: containers/ の mount は全コンテナのログを見せる。他 project の行
  （ログ基盤自身・別の試験環境）が混ざらないことを固定する。
- **`avp.logging=app` でも stderr・JSON でない行・壊れた JSON は infra（unstructured）**: アプリの
  起動失敗の traceback や alembic の出力を app の mapping に入れると型不整合で bulk item が 400 になる。
- **型修復**: boolean に `"yes"`、keyword に object、未知のキー、アプリが書いた Collector のフィールドを
  `attributes.collector_moved` へ退避し `collector_errors` に名前を残す。これをしないと 1件の 400 が
  retry 上限まで再送され、同じ chunk の再送回数を消費する（ADR-0040 §5）。
- **`@timestamp` を record から外し record の時刻へ移す**: 出力側（`time_key`）が `@timestamp` を1つだけ
  書く。重複キーは ingest pipeline 経由で bulk 全体を 400 にする（実測）ので、構造的に起きない形にする。
  不正な値は Docker の時刻へ置換し `@timestamp_replaced`。
- **`_id` にできない `event_id` の退避**: 512 bytes を超える `_id` は item ではなく bulk の **request 全体**が
  400 になり、同じ chunk の正常な行も 72 回の再送（実測 約2時間50分）の末に破棄された（隔離環境で実測、
  正常2件を含む3件が `dropped_records_total`）。Collector で退避して自動 ID にする。
- **app 系統の記録は必ず `event_id` を持つ**: Fluent Bit 5.1.2 の opensearch output は `id_key` の値が無い
  record に**直前の record の `_id`** を使い回す（action 行を作り直さない。`plugins/out_opensearch/opensearch.c`）。
  隔離環境で、退避した長い ID の行が直前の行と同じ `_id` で送られ 409（成功扱い）になり**黙って消えた**。
  JSON でない行・退避した行には Collector が `collector-…` の ID を付ける。
- **ミリ秒の丸め**: 出力側は record 時刻の `tv_nsec` を切り捨ててミリ秒を書く。`.001` を double にすると
  `.000999…` になり 1ms 早い時刻が保存された（隔離環境で実測）。stdout も同じ切り捨ての iso8601 で検査する。
- **Lua が書くキーは行き先の mapping の部分集合**（I-7）: `dynamic: false` なので、mapping に無いキーを
  Collector が書いても黙って検索できないだけで、どこもエラーにならない。
- **partial を結合した長い行で filter 列が止まらない**（I-15）: Docker と同じく 16KiB の partial に分けた
  20万字規模の行（英数字・hex・`eyJ`・`http://`・`[parameters: `・PEM 見出しの繰り返し）を app と infra の
  両方に流し、後続の 50 行が全て出ること、全体が 60 秒以内に終わることを確かめる。修正前は1行目で
  O(n²) になり 120 秒で打ち切られた（実測）。上限を超える行が app の文書にならず、infra に
  `line_too_long`・`truncated` で1件だけ出ることも固定する。`exit_on_eof` が長い行の途中で終了する
  （5.1.2、実測）ので、この試験だけ `buffer_chunk_size` を大きくして渡す（`_run` の `tail_overrides`）。
- **線形に書き直した規則が同じものを伏せる**（I-15）: userinfo（`+` を含む scheme、空の user）、URL の
  query・fragment、SQL の `[parameters: …]`（閉じていないものも）、PEM（END の無いものも）、JWT。
  書き直しで伏せ漏れが出ないことを、値が出力に残らないことで確かめる。
- **追加の安全化と切り詰め**: 整形器の取りこぼし（`Authorization: Bearer …`、DSN の userinfo）が
  OpenSearch へ届かないこと、unstructured 行が `UNSTRUCTURED_LINE_MAX_BYTES` 以下になることを固定する。

## 4. `tests/contract/test_compose_logging.py`（と `test_research_worker_compose.py` の変更）

- **全サービスが json-file を max-size 20m × max-file 5 でローテーションし、`labels`/`tag` で attrs を
  付ける**: 現状は全コンテナがローテーション無し（最大 約77MB）。attrs が無い行は Collector が compose project
  で絞れず捨てる。`mode: non-blocking` は黙って欠損するので blocking のままであることも固定する。
- **アプリ（app / worker イメージ）のサービスだけが `avp.logging=app` を持ち、`AVP_SERVICE_NAME` が
  compose のサービス名**: label がずれると postgres 等の行が app の mapping に入り、逆にアプリの行が
  infra へ流れて検索できなくなる。`service_name` と `compose_service` が一致することで照合できる。
- **`AVP_ENVIRONMENT` に既定値を持たせない**（I-5、レビューで仕様変更）: 既定 `dev` だと、本番の `.env` に
  書き忘れたとき本番の行が `environment=dev` で prod の index に入り、推測値で埋めない契約（log-contract §2）に
  反する。空文字なら整形器が `unknown` にすることも同じ test で固定する。
- **API の起動コマンド**: uvicorn の CLI 起動は独自の handler と query 付きの access log を出す
  （ADR-0040 §1）。モジュール `apps.api.serve` は担当A が作るので、ここでは存在を検査しない。
- `test_research_worker_compose.py` は research-worker の env を「app-env ＋ provider の設定だけ」に
  固定している（秘密の混入を止める検査）。ADR-0040 §3（Accepted）でアプリの各サービスに
  `AVP_SERVICE_NAME`（秘密ではない）を足すので、許可する集合にそれを加え、値がサービス名であることを検査する。
  `AVP_ENVIRONMENT`・`AVP_LOG_FORMAT` は `x-core-env` 経由なので既存の「app-env と同じ集合」に含まれる。
