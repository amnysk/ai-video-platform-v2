# テスト設計の根拠: Research の Provider Adapter（ADR-0036 §3）

各テストが**なぜテストになったか**を残す。守るものは 3 つ: (1) 外部ページ・検索結果にあった URL を辿って
内部ネットワークへ到達しないこと（SSRF）、(2) 取得できていないものを「本文確認済み」にしないこと、
(3) 通常のテスト・CI が実 Provider・実ネットワークへ出ないこと（INV-18）。実在の事故ではなく
設計上の脅威から起こしたテストなので、「落ちたら何が起きるか」を書く。

## `tests/unit/test_research_ports.py`（domain の値オブジェクト）

| テスト | なぜ必要か / 落ちたら何が起きているか |
|---|---|
| `test_only_a_complete_nonblank_text_is_a_confirmed_body` | `body_confirmed` は「本文確認済み」の**唯一の定義**。切り詰め・失敗・テキスト無し（PDF）・空本文が真になると、根拠の無い資料が Evidence の裏付けとして数えられる |
| `test_value_objects_are_frozen` | 検索結果・取得結果が途中で書き換わると、保存した Artifact と判定の根拠が食い違う |
| `test_search_hit_carries_observations_with_their_observation_time` | 統計は観測時刻なしでは増加速度も鮮度も語れない（ADR-0036 §4）。`None` の購読者数を 0 と区別する |
| `test_ports_are_structural_protocols` | Adapter は Port を継承せず構造で満たす（domain が infrastructure を import しない。INV-6） |

## `tests/unit/test_url_guard.py`（SSRF 規則、実 DNS なし）

| テスト | なぜ必要か |
|---|---|
| `test_blocked_urls_are_rejected_before_any_connection`（約 80 例） | loopback / private / link-local / metadata / carrier-grade NAT / IPv4-mapped・6to4・NAT64 / 10 進・16 進・8 進・省略形の IP / 全角数字 / userinfo / scheme（file・ftp・gopher・data）/ 壊れた URL。**書き方を変えれば通る**穴が 1 つでもあると、Provider の返した URL 経由でメタデータ（`169.254.169.254`）や内部 API を読まれる |
| `test_rejection_reasons_are_machine_readable_codes` | 拒否理由は文字列判定ではなくコードで扱う（failure-policy: 型で分類する） |
| `test_rejection_never_echoes_userinfo` | URL の `user:pass@` を例外・ログ・結果に残さない（AGENTS.md §9） |
| `test_literal_ip_hosts_are_never_sent_to_the_resolver` | IP リテラルは名前解決しない（解決結果に差し替えられる余地を作らない） |
| `test_a_name_that_resolves_to_a_private_address_is_rejected` | 公開名が private に解決される場合と、**公開と private が混在**する場合の両方を拒否。混在を許すと、どれに接続するかを DNS 側に選ばれる |
| `test_public_urls_are_accepted_and_pinned_to_the_checked_address` ほか | 拒否しすぎて調査が動かなくなるのも欠陥。公開 URL・IDN・公開 IPv6・mapped の公開 IPv4 は通り、**検査した IP を接続先として返す**（接続固定の前提） |
| `test_resolution_failure_is_not_a_policy_violation` | DNS 失敗（通信障害、retry できる）を恒久の方針違反と混ぜない |
| `test_blocked_address_reason` | 境界（172.15 / 172.32、100.63 / 100.128 は公開、100.64/10 は非公開）を機械的に固定 |

## `tests/unit/test_http_content_fetcher.py`（`httpx.MockTransport` + Fake resolver）

`request.url.host` が**接続先**、`Host` ヘッダが論理名。実ネットワークには出ない。

| テスト群 | なぜ必要か |
|---|---|
| 成功・HTML 抽出・sha256・redirect_chain | 成功の形（hash は取得 bytes のもの）。script / style を除くが解釈しない |
| `test_the_connection_is_pinned_...` / `test_dns_rebinding_cannot_steer_...` | **DNS rebinding**: 検査時は公開 IP・接続時は private を返す DNS でも、接続は検査した IP へ固定される（解決は hop に 1 回、以後 IP リテラルへ接続）。TLS の SNI・証明書検証は元の名前 |
| `test_a_public_host_cannot_redirect_into_the_internal_network`（18 例） | 公開ホストが 302 で localhost / private / metadata / 各種 IP 表記 / file / ftp / gopher / data / userinfo へ誘導しても、**各 hop で同じ検査**が走り、2 つ目のリクエストは作られない |
| リダイレクト回数（5 回まで OK・6 回目は拒否）・`https`→`http` 降格・Location 無し | 無限ループ・降格・不正な転送の遮断 |
| `test_blocked_start_urls_never_reach_the_transport` | 禁止 URL では transport が 1 度も呼ばれない |
| `test_dns_answers_mixing_public_and_private_addresses_are_refused` | 複数 A の混在は全体拒否 |
| サイズ: `Content-Length` 超過・偽装のストリーム超過・ちょうど上限 | 宣言が大きければ**読まずに**失敗。宣言が小さくてもストリームで数え、先頭だけを `truncated`（確認済みではない）で返す。2 MiB を境に 1 バイトずれる退行を検出 |
| `test_a_gzip_bomb_is_stopped_by_the_decoded_size` ほか圧縮 | 数十 KB が数十 MB に展開される bomb を、展開後サイズで止める（自前の上限付き展開）。壊れた・途中で切れた圧縮は確認済みにしない |
| content-type（許可外は本文を**読まずに**拒否）・PDF | PDF はテキスト化しない（依存を増やさない。ADR-0009）ので `failed` / `no_text_extractor`。テキストの無い資料を「本文確認済み」にしない |
| 文字コード（宣言・meta・未知・不正バイト） | 化けた本文で主張を照合しない。未知の codec 名（`zlib` など）を素通しにしない |
| timeout（`ReadTimeout` 等の分類、遅いストリームが全体期限で終わる） | 1 バイトずつ返す相手に居座られない。期限は取得全体（hop をまたぐ）に掛かる |
| `test_cookies_set_by_a_server_are_never_sent_back` | Set-Cookie を受けても保存・送信しない（セッションを持ち込まない） |
| `test_the_client_never_uses_ambient_proxy_or_netrc_settings` | 環境の `HTTP(S)_PROXY` に流すと固定した接続先を迂回される |
| `test_instructions_inside_a_page_are_returned_as_data_and_never_acted_on` | 外部ページの命令文は**データ**。本文中の URL を取得しに行かない |
| `test_userinfo_is_redacted_in_the_result` | 結果に資格情報を残さない |

## `tests/unit/test_research_fakes.py`（Fake と固定コーパス）

| テスト | なぜ必要か |
|---|---|
| 検索の決定性・順位・`max_results` と `truncated` | Fake は通常テストの唯一の Provider。揺れると下流のテストが不安定になる |
| YouTube: `cost_units == 102`・観測時刻・欠けた値は欠けたまま・`requested_*` | 本物の Adapter と**同じ形**の出力（差があると Fake でだけ通るコードができる） |
| `test_the_corpus_contains_each_hard_case` | 転載（同一 sha256）・snippet だけ（403）・timeout・PDF・切り詰め・リダイレクト・異説・年代ずれ・量化子違い・因果を明言しない資料が**コーパスに実在する**こと。下流（独立性・裏付け判定・量化子検査）のテストの前提 |
| `test_a_snippet_only_document_cannot_be_fetched` | snippet に年があっても、本文を取得できなければ本文確認済みではない |
| `test_fakes_never_touch_the_network` | `socket` を落とす形で、Fake が実ネットワークに出ないことを固定（INV-18） |
| 障害注入・呼び出し履歴・`NotConfiguredSearchProvider` | 429・例外・値としての失敗を再現できる。Web 検索は選定未了で、呼ぶと `needs_input`（推測で外部サービスに固定しない） |

## `tests/unit/test_research_errors.py`（失敗クラスへの写像）

分類（retryable / needs_input / permanent）を**型**で固定する。domain の具体型名は別ブランチで追加されるので、
基底クラスへの isinstance で検査する。429・5xx・timeout・network は retryable、禁止 URL・4xx・型違い・サイズ超過は
permanent、quota・認証・未設定の Provider は needs_input。未知の例外は握りつぶさない。

## `tests/unit/test_youtube_search.py`（`httpx.MockTransport`）

| テスト | なぜ必要か |
|---|---|
| 全体の流れ・quota 単位（100 + 1 + 1） | 予約台帳に記録する単位の根拠。呼び出し順・パラメータを固定 |
| `test_region_and_language_are_filters_...` | `regionCode` / `relevanceLanguage` は絞り込み・重みづけ。人気・言語・視聴者層を断定しない。`videoDuration` は送らない（Shorts 判定に使わない。ADR-0036 §4） |
| 補助呼び出しの失敗は警告に落とす・費用は数える | 検索に 100 units 使った後の失敗で結果を捨てない。送った呼び出しは課金されたものとして数える（INV-15） |
| エラー分類（quota / 429 / 5xx / 400 / 403 / 401 再取得 / 通信失敗） | 既存 uploader と同じ作法。token・応答本文を例外に出さない |
| ISO 8601 の再生時間 | 読めない値を推測しない（`None`） |

## `tests/architecture/test_research_no_live_network.py`

| テスト | なぜ必要か |
|---|---|
| `test_domain_research_is_pure` | domain が HTTP・socket を持つと Port が I/O を抱える（INV-6） |
| `test_only_the_http_fetcher_imports_an_http_client` / `..._sockets_...` | 通信の入口を数えられる場所（1 ファイル）に閉じ込める。Fake は通信手段を持てない |
| `test_research_code_has_no_provider_endpoint_strings` | YouTube / Google / fal のエンドポイントは `infrastructure/youtube/` 等にだけ書ける（INV-18） |
| `test_every_http_call_is_in_send_pinned_...` ほか AST 検査 | 取得の公開入口は `fetch` 1 つ。httpx への送信は `_send_pinned` 1 か所で、`GuardedTarget` を要求し、呼び出し側は先に `UrlGuard.check` を呼ぶ。リダイレクトは hop ごとに検査する。**guard を通らない取得経路を後から足すと落ちる** |
| `follow_redirects=False` / `trust_env=False` | httpx に自動でリダイレクトを追わせない・環境のプロキシを使わない |
