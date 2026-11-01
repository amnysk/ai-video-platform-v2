# v3 Phase 3 独立レビュー（Sonnet E）

対象: `claude/v3-phase3-integration` @ `7fd36e4`（`git diff 7ae20e8..7fd36e4`、133 ファイル、+12584/-142）。
レビュアーは実装していない。コードは直していない。証拠のテストは `tests/unit/test_review_p3_*.py`（別コミット）。
判定の基準は AGENTS.md（TDD / §2 テスト非緩和 / §3 無関係な変更 / §6 設計先行 / §7 設計書同時 / §8 定義1箇所 / §9 お金と外部副作用）。

## 結論

| 問い | 判定 |
|---|---|
| (a) 4 flag すべて OFF で本番（e3c9555）にデプロイしてよいか | **可（所有者の API トークン設定を前提に）**。BLOCKER 無し。起動・replay・render_input_hash・migration の互換は確認した。意図した変更（API の変更系が token 必須・503、release 承認の `approval_ref` 必須、integration の隔離宣言）以外の挙動差は見つからなかった |
| (b) 機能として完成しているか | **未完成**。品質ゲートに BGM / 地図が**結線されていない**（B1・B2）。このため `AVP_QUALITY_GATE_ENFORCE=true` にした瞬間に全 Episode が REVIEW になり自動投稿が止まる。BGM / GEO を ON にするのは、B1〜B3 を直すまで不可 |

## 所見

### BLOCKER（flag を ON にする前に直す。flag OFF の deploy は止めない）

**B1. 品質ゲートに BGM / 地図の producer が登録されていない。**
`infrastructure/quality/wiring.py:18-27` の `build_quality_producers()` は `()` を返す（docstring は「他の枝がここへ足す」）。
BGM（INV-138）・地図（INV-142）の純関数 `bgm_quality_checks` / `map_quality_checks` は、本番コードのどこからも呼ばれない
（`grep -rn "bgm_quality_checks\|map_quality_checks" apps workers infrastructure` → 定義のみ）。
INV-138 の本文は「永続化と gate への接続は未配線」と自認しているが、parent の統合でも接続されていない。
- 失敗シナリオ: `GEO_MAPS_ENABLED=true`（`require_quality_gate` により `AVP_QUALITY_GATE_ENFORCE=true` が必須）で、Render が誤った地図を拒否し
  元のシーン動画で完成動画を作る（`workers/render/activities.py:695` のログは「the quality gate refuses the upload」と言う）。
  gate は地図の証跡を一切読まないので、**拒否された地図は gate から見えない**。INV-140 の「投稿を許さない」は、純関数が BLOCK を返すところまでしか機械検査されていない。
- 証拠: `tests/unit/test_review_p3_unwired_quality.py::test_review_production_gate_has_bgm_and_map_producers`（FAIL）。
  既存の `test_no_branch_producers_are_registered_yet`（tests/unit/test_quality_approval_and_wiring.py。再レビュー時点で解消済み） は `build_quality_producers() == ()` を**固定**している
  （結線時に更新が要る。ADR-0081 §7 が根拠なので §2 違反にはならない）。
- 修正案: `CheckKey.BGM` / `CheckKey.MAP` の producer（証跡を MinIO から読み、`evidence_ref` を持つ記録にする）を `build_quality_producers` に足す。
  「証跡を読めなかった」は例外にして記録を作らない（INV-138 の約束）。結線の end-to-end テスト（Render → gate → BLOCK）を足す。
  parent の worktree `claude-v3-p3-wiring` が既にこれを扱っているように見える（観測のみ。未確認）。

**B2. Render が使用宣言（`quality_usage_declarations`）を書かない。`ENFORCE` ON だけで全 Episode が REVIEW。**
`SqlQualityRecordWriter.declare_usage` の呼び出し元は本番コードに無い（テストのみ）。`domain/quality/records.py:305-314` は、宣言が無い機能を
`USAGE_DECLARATION_MISSING`（REVIEW）にする（INV-127、意図した fail-closed）。結果、BGM も地図も使っていない通常の Episode でも、
`AVP_QUALITY_GATE_ENFORCE=true` では自動投稿が `quality: report_needs_review` で止まり、手動投稿は owner の承認が毎回要る。
e3c9555 では ENFORCE=true で PASS した動画が、この統合では PASS しない（回帰）。
- 証拠: `tests/unit/test_review_p3_unwired_quality.py::test_review_render_declares_usage_for_the_gate`（FAIL: Render 後の宣言は 0 件）。
  `tests/unit/test_quality_gate_service.py::test_without_usage_declarations_the_gate_refuses_as_review` が現挙動を固定している。
- 修正案: Render の `_store_final` 後（final_video 行の記録と同じ transaction か、直後）に、BGM / 地図それぞれ使った（証跡 ref つき）か使わない（ref なし）かを宣言する。
  flag OFF では `used=false` を宣言するのが INV-130（OFF は記録を読み書きしない）と衝突しないか ADR で決める（OFF は現状どおり gate 自体を通らないので衝突しない）。

### MAJOR

**M1. BGM の帰属表示（クレジット）が YouTube の説明欄に入らない。** `workers/upload/activities.py:445` と `infrastructure/quality/service.py:166` は
`build_upload_metadata(upload_key=..., script=script)` で `credits=` を渡さない。`BgmResolution.credit_lines` は証跡（`workers/render/activities.py:672`）に残るだけ。
CC-BY など `attribution_required` の曲を使うと、ライセンス条件（帰属）を満たさないまま公開される。ADR-0083:169 が「未配線（ADR-0077 から継続）」と自認。
BGM を ON にする前に必須。`bgm_quality_checks` が見るのは「証跡のクレジット = カタログ原文」で、**説明欄に載ったか**ではない。

**M2. 所有者トークンと agent トークンは同一ファイルに入り、同じ OS ユーザーが読める。** `docs/operations/api-auth.md` の手順は
`~/.config/avp2/api-tokens`（0600、所有者 = 運用ユーザー）に owner と agent の両方を書く。本番ホストでエージェントは同じユーザーで動くので、
エージェントは owner トークンを読んで REVIEW を承認できる。ADR-0080 は承認参照を「認証ではない」と明記しているが、トークンについては
「owner だけが REVIEW を承認できる」と書いていて、**この限界を書いていない**。コードの不具合ではなく、保証の過大申告。
修正案: owner トークンを別ユーザー/別ファイル（エージェントが読めない場所）に置く運用を必須として書く。または限界を ADR と INV-125 に明記する。

**M3. release 承認の `enables` は自己申告で、省略すると所有者専用の関門の検査が空振りする。** `domain/release_preflight.py:1495`
（`doc.get("enables", [])`）。委任（`AGENT-…:OWNER-…`）の承認が新 flag を ON にするデプロイを承認しても、`enables` を書かなければ PASS する。
preflight は対象環境の実効 flag（`AVP_QUALITY_GATE_ENFORCE` / `BGM_ENABLED` / `GEO_MAPS_ENABLED` 等）を見て関門を導いていない。
ADR-0080 §A.4 は「任意」と明記しているので設計どおりだが、「委任は新機能 ON を通さない」という INV-123 の保証の実効性は書き手の誠実さに依存する。
修正案: preflight が対象の env/compose から flag の ON/OFF を実測し、ON なら `ENABLE_NEW_FEATURE` を暗黙に要求する。

**M4. 出荷物としての deploy 手順が欠けている。** `AVP_API_TOKENS_FILE` は `compose.yaml` に無く、`docs/operations/release-runbook.md` にもトークン設定の手順が無い
（`docs/operations/api-auth.md` にだけ在る）。デプロイ後、所有者が override を足すまで `POST /episodes` 系はすべて 503。
日次 Schedule・watchdog は API を経由しないので自動運転は止まらない。runbook の事前条件に「トークンの作成と override」を足す。

### MINOR

- **m1.** `apps/api/auth.py:126-132` `get_api_tokens` は GET を含む `/episodes`・`/research` の**全リクエスト**でファイルを読む（同期 I/O）。ファイルが壊れていると全リクエストが ERROR ログ（`_refuse`）を出す。
  再現: `AVP_API_TOKENS_FILE=/nonexistent` で `GET`。`SAFE_METHODS` 判定を先にして、トークン読み込みを変更系だけにする。未設定（既定）では `_refuse` を呼ばないので flag OFF の本番は静か。
- **m2.** ADR-0080 §B.1 は「secret ファイルか env」から読むと書くが、実装はファイルだけ（`infrastructure/config.py` `api_tokens_file`）。設計書を実装に合わせる（§7）。
- **m3.** 追記専用は UPDATE のトリガだけ（`0019_...py:_postgres_guards`）。DELETE / TRUNCATE は止めない。さらに全表の FK が `ON DELETE CASCADE`（Episode 削除で監査記録が消える）。
  「追記専用」の表現は「UPDATE 拒否」に狭めるか、DELETE も拒否する。
- **m4.** `quality_manual_approvals` の複合 FK は `(report_id, final_video_sha256)` で `episode_id` を含まない。同じ sha256 を持つ別 Episode の報告に承認を結べる（同一バイトの動画が 2 Episode にある場合のみ）。`episode_id` を FK に含める。
- **m5.** `get_upload_approver` の `approver_id` は `api-owner-token` / `api-agent-token` の定数。承認記録から「どのトークンの持ち主か」は分からない（トークンが 1 owner 1 agent なので現状は足りる）。
- **m6.** 承認は workflow に渡らない。manual 経路は API で承認 → `start_upload_workflow` で、upload 工程は起動時点の現行 final_video を再解決する。API 呼び出しから起動までの間に再 render が入ると、承認した hash と投稿される hash が別になりうる（既存の ADR-0076 から続く窓。狭い）。
- **m7.** `tests/support/production_guard.py` の `_APPTEST_PROJECT`（正規表現）と `run-e2e.sh` の case パターンは同じ規則を 2 箇所に持つ（§8）。一致を検査する contract テストが無い。
- **m8.** ガードは `os.environ` だけを見る。`Settings()`（`tests/support/minio.py` / `fake_upload_worker.py`）は `.env` も読むので、`.env` が本番を指していて環境変数が未設定のとき、ガードは何も見ない。ただし MinIO の integration は `MINIO_ENDPOINT` 未設定なら skip なので、実害の経路は狭い。
- **m9.** `QualityGateService` は呼び出しのたびに `quality_reports` を 1 行足す（`upload_gate` の再試行・手動 POST の連打で増える）。上限・重複排除なし。
- **m10.** `tests/architecture/test_geo_boundaries.py` の docstring（`test_production_billing_and_entrypoints_do_not_import_geo`: "ADR-0071 (not wired yet)"）が古い。許可リスト `GEO_CONSUMER_EXEMPT` に `infrastructure/geo` を足したのは ADR-0084 が根拠。

## 確認した範囲（問題が見つからなかった項目）

### 1. flag すべて OFF の本番互換
- 起動: worker イメージ内で `apps.api.main` と全 `workers.*.run_worker` を import し、`Settings()` の 4 flag と `api_tokens_file` が既定（False/None）、`build_quality_service` が `None`（MinIO クライアントを作らない）。新しい必須 env は無い（`Settings` の追加は `api_tokens_file: str | None = None` の 1 個）。
- replay: `workers/*/workflows.py`・`contracts/*_activities.py`・Activity 名・入出力 dataclass の差分は 0（`git diff --name-only` に無い）。CI の `temporal-replay` ジョブ相当のテストを明示実行して全部 PASS（結果は末尾）。
- `render_input_hash`: `bgm=None` / `geo=None` のときキーごと入れない（`domain/render/identity.py`）。既存の golden hash テストが通る。`BGM_MIX_VERSION` 1→2 と ducking 係数の変更は BGM 入りの plan にだけ効く（OFF では BGM plan が無い）。production の入力 hash は動かない（INV-116/137 のテストあり）。
- upload: `build_upload_metadata` は `AVP_UPLOAD_DESCRIPTION_COMPOSED` OFF で従来の `build_youtube_metadata(...)` と同じ引数・結果。`compute_upload_key` は変更なし。
- 品質: `AVP_QUALITY_GATE_ENFORCE` OFF では `get_quality_service()` が `None`・`PipelineActivities._quality_service` が `None`で、手動投稿も upload_gate も従来どおり記録を読み書きしない（INV-130 のテストあり）。`QUALITY_RULE_VERSION` の 2 への更新は ENFORCE ON の経路にだけ効く。
- migration 0019: 新しい表 4 個の追加のみ（既存の表・列・制約・索引に触れない）。e3c9555 のコードはこれらの表を知らないので、旧コードが走っている間に適用してよい。`downgrade` は行が 1 つでも在れば RuntimeError で拒否（空なら表を落とす）。単一 head（`0019`）。
- 課金: 新しい fal / Codex / YouTube の呼び出し経路は無い。Render の地図・BGM は ffmpeg とローカルファイルのみ（`infrastructure/geo` はネットワーク・有料 provider を import しない: アーキテクチャテスト）。Production の地図 preflight は有料呼び出しの**前**に止める。
  再 render が必要になるのは geo / bgm を ON にした Episode だけ（identity が変わる）。render は fal を呼ばない。`compute_upload_key` が変わらないので二重投稿の防止は従来どおり。

### 2. API 認証（ADR-0080）
- 両ルーターに `dependencies=[Depends(require_api_actor)]`。`/healthz` 以外の `@router.*` は POST と GET のみ。websocket・mount・method override は無い。HEAD/OPTIONS は `SAFE_METHODS` だが、変更系ルートに HEAD/OPTIONS は存在しない（TRACE 等は未許可で認証が要る）。
- 比較は sha256 digest + `hmac.compare_digest` を全件、トークンは保存・ログ・応答に出ない。ファイルは 0600 以外・通常ファイル以外・重複・短い・未知の actor を全て拒否（503）。大文字小文字違いの `bearer` は 401（fail closed）。
- `tests/conftest.py` の autouse override は `apps.api.main.create_app` の戻り値にだけ作用し、`real_api_auth` マークで外れる。`app = create_app(wire_quality_service=True)`（本番の app）には効かない。override が実際の穴を隠す経路は見つからなかった。
  `from apps.api.main import create_app` を test の先頭で束縛するテストは override を受けないので、落ちて気づける側に倒れる。
- 実トークン → actor → `get_upload_approver` → wired gate の連鎖は、既存テストが `get_upload_approver` を override していたため未検査だった。`tests/unit/test_review_p3_token_to_approval_e2e.py` で確認: agent トークン = 403 で何も起動しない・承認 0 件、無/誤/小文字 bearer = 401、owner トークン = 202 で `approver_kind=owner` の承認が記録される（3 件 PASS）。

### 3. 承認の種別
- `OwnerGate` の全関門（予約の手動確定・provider 保留の解放・breaker・waiver）は `require_gate_allowed` で `AGENT_DELEGATED` と種別なしを拒否する。`format_approver` は名前に種別の接頭辞を持たせない。旧い行（接頭辞なし）は `parse_approver` で `None`＝所有者承認ではない。
- 変更された既存テスト 2 件は正当: `test_reconcile_reservations_script`（`actor` が `owner:me` に）は ADR-0080 §A.5 が先に決めた保存形式、`test_release_preflight._approval`（`approval_ref` を足す）は ADR-0080 §A.4 の新必須キー。どちらも期待を緩めず、仕様の追加に追随したもの。
  `test_bgm_ffmpeg_graph.EXPECTED_TAIL`（ducking 係数）は ADR-0083 §（実測で係数変更・`BGM_MIX_VERSION` 2）が根拠。`test_geo_boundaries` の許可リスト拡張は ADR-0084。検出器を `tests/support/import_scan.py` へ移したのは検出を**強める**変更（`from x import submodule`・相対 import・動的 import を足す）。

### 4. QualityGate fail-closed（構築を試みて破れなかった）
- 他 hash / 他 input_hash / 他 rule_version / 期限切れ / 未来 / 壊れた期間の記録は `select_fresh_check_sets` が「無い」にする（SQL に依存しない）。現行でない（`superseded_at` あり）final_video は `current_final_video` が返さない。
- 必須の検査が欠ける・producer が例外・保存失敗・報告が保存できない・`evaluate` の例外は全て拒否（`QualityGateService.evaluate` が包む）。承認を記録できなければ workflow を起動しない（409）。
- 複合 FK は `(report_id, final_video_sha256)`（m4 を除き有効）。UPDATE はトリガで拒否（DELETE は m3）。
- 破れなかったが、B1/B2 により「記録が揃って PASS する」経路自体が本番に存在しない（test の fake producer / 手書き宣言でしか通らない）。

### 5. integration ガード（ADR-0079）
- 宣言が無い・綴り違い・空・前後空白は全て例外（収集時）。`ci-ephemeral` は `GITHUB_ACTIONS=true` かつ loopback のみ。`apptest:` は `avp2-oslog-c*` とサービス名 `temporal/postgres/minio` のみで、ホストに解決される名前を拒否。Temporal は `default` namespace に日次 Schedule が在れば拒否（describe のみ、判定不能も拒否）。
- 直書き禁止は `tests/architecture/test_integration_no_hardcoded_addresses.py` が検査。`test_pipeline_schedule.py` は直書きをやめ `TEMPORAL_ADDRESS` が無ければ skip。
- CI への影響: `ci.yml` の integration / alembic ジョブに `AVP_INTEGRATION_ISOLATION: ci-ephemeral` が入っている。他のジョブは integration を走らせない。接続先は全て localhost。
- 限界（ADR に明記済み）: PostgreSQL / MinIO は静的な宣言と接続先の規則だけに依存する。m8 を参照。

### 6. 地図・BGM
- 地図の座標は gazetteer からのみ（`manifest_builder` / `check_map_accuracy`。誤座標の manifest は BLOCK のテストあり）。時代錯誤・出典の解決・評価不足・同名の曖昧は `gate_map` が PASS にしない。地図に fal は使わない（`infrastructure/geo` はネットワーク・provider を import しない）。拒否された地図は描かず元のシーン動画を残す。ただし拒否が投稿を止めるのは B1 が直ってから。
- BGM: ライセンスは描画のたびに `resolve_bgm` が再検査。`register_track` は sha256 で重複を排除し、承認は運ぶだけ。`object_key` は `..` と絶対パスを拒否。ラウドネス / 発話中の差は実測で判定。
- INV-121〜145 の本文が挙げる「機械検査」のテスト（`tests/...::name`）は全て実在する（スクリプトで確認、欠落 0）。ただし INV-138 / INV-140 / INV-142 の「投稿を許さない」「gate に接続」は純関数の検査までで、end-to-end の検査は無い（B1）。

## テスト結果（7fd36e4）

| 検査 | 結果 |
|---|---|
| `ruff check .` / `ruff format --check .` | All checks passed / 1050 files already formatted |
| `pyright` | 0 errors, 0 warnings |
| `pytest tests/unit tests/contract tests/architecture -q` | **6268 passed**, 157 warnings（parent の数と一致。477 s。レビュー用テスト追加前） |
| Temporal replay（CI `temporal-replay` ジョブと同じ 6 ファイル） | 83 passed |
| 隔離 integration（`run-e2e.sh`、project `avp2-oslog-c-p3e`、`pytest tests/integration -m integration -rs`） | **166 passed, 6 skipped**（rc=0、445 s）。`-m "not integration"` は 0 件（マーク漏れなし） |
| 追加したレビュー用テスト | `test_review_p3_token_to_approval_e2e.py` 3 passed／`test_review_p3_unwired_quality.py` 2 **failed（意図）** |

skip 6 件は全て `tests/integration/test_logging_collector_lua.py:43`「docker が無い」（test-runner コンテナの中に docker が無い。隔離スタックの設計上の既知。Lua collector の検査は別手段）。
migration 0019 は隔離 PostgreSQL に `migrate` サービスで適用済みで、`test_quality_records_postgres.py` と `test_migration_against_postgres.py` が通った（upgrade → 制約 → 追記専用トリガ → downgrade の拒否/実行）。

本番には触れていない（`avp2-*` コンテナ・:7233/:5432/:9000/:8000 に接続なし）。隔離スタックは検査後に `down` 済み。

## 推奨する次の手順

1. B1・B2 を直す（producer の登録 + Render の使用宣言 + Render → gate → BLOCK の end-to-end テスト）。`test_review_p3_unwired_quality.py` が緑になることが完了条件。
   その際 `test_no_branch_producers_are_registered_yet`（`== ()`）は ADR-0081 §7 を根拠に更新する。
2. M1（説明欄のクレジット）を BGM を ON にする前に直す。
3. flag OFF のまま deploy する場合は、M4（トークン設定の runbook への追記）を先に行う。
4. M2・M3 は ADR-0080 / INV-123・125 の保証の書き方を実態に合わせる（コード変更は必須ではない）。

---

# Re-review（`7fd36e4..ba1e895`、Sonnet E）

レビュー worktree に `claude/v3-phase3-integration`（ba1e895）を `git merge --no-ff` した（衝突は私の証拠テスト 2 ファイルのみ。wiring 側の更新版を採用）。

## 結論

| 問い | 判定 |
|---|---|
| (a) 4 flag OFF の本番 deploy | **現状の head ではそのまま deploy できない（新 BLOCKER R1）**。release preflight の `migration_compat` が migration 0019 を BLOCKED にする。コードの挙動（flag OFF の互換）は問題なし。R1 を直せば可。あわせて owner の token ファイルの用意が preflight の必須条件になった |
| (b) 機能の完成度 | **B1・B2・M1 は解消**（実素材で PASS を確認）。BGM / GEO を ON にするには、R2（BGM ON でも説明欄の構成 flag が OFF だとクレジットが落ちる）と R3（flag を container に渡す経路が compose に無い）が残る |

## 前回所見の状態

| ID | 状態 | 根拠 |
|---|---|---|
| B1 producer 未登録 | **解消** | `build_quality_producers(settings, store)` が `BgmCheckProducer` / `MapCheckProducer` を返す。証跡が無い/読めない/別動画は例外（記録されず REVIEW）。`test_review_p3_unwired_quality.py` は緑 |
| B2 使用宣言が書かれない | **解消** | Render が `final_video` 記録後に `declare_render_usage`（ENFORCE ON のときだけ writer を持つ。失敗は非致命＝REVIEW に倒れる）。下記の実素材で `bgm=used(ref あり)` / `map=not used` を確認 |
| M1 クレジット | **部分解消（R2）** | `BgmCreditSource` を upload と gate の説明欄 lint が共有（INV-148）。ただし `AVP_UPLOAD_DESCRIPTION_COMPOSED` が OFF（既定）だと `build_upload_metadata` が credits を使わず、`credit_lines` は `()` を返す |
| M2 トークン分離 | **解消（限界は明記）** | 任意の `AVP_API_OWNER_TOKEN_PATH`（owner だけの別ファイル。設定時は共有ファイルの `owner:` 行を拒否、owner ファイルが読めなければ全拒否）。同一 UID では分離にならない旨を ADR-0080 / api-auth.md に「残るリスク」として記載 |
| M3 `enables` 自己申告 | **部分解消（R3）** | `enables` が必須（`[]` 明示）。対象環境の flag（`FEATURE_FLAG_ENV_KEYS`）が ON なら、委任承認は BLOCKED、本人承認でも `enable_new_feature` を `enables` に書かないと BLOCKED。ただし flag の値を読む先は repo の `.env` のみで、`compose.yaml` が container へ渡すのは `BGM_ENABLED` だけ |
| M4 deploy 手順 | **解消（ただし deploy の前提条件が増えた）** | `compose.yaml` の api に `AVP_API_TOKENS_PATH` と bind mount、runbook に手順、preflight の必須チェック `api_token_file`（存在・0600・所有 UID = `AVP_UID`・owner と agent の token）。新しい preflight 前提は「owner が token ファイルを `~/.config/avp2/api/tokens` に作る」こと |
| m1 GET で毎回ファイルを読む | 解消 | `get_api_token_source` は変更系のときだけ読む |
| m2 ADR と実装の不一致（env） | 解消 | ADR-0080 を実装（ファイルのみ）に合わせた |
| m3 DELETE/TRUNCATE | 解消 | 4 表に DELETE / TRUNCATE 拒否トリガ（行ごと/文ごと）。FK は `ON DELETE RESTRICT`。ただしこの編集が R1 を生んだ |
| m4 承認 FK に episode_id が無い | 解消 | `(report_id, episode_id, final_video_sha256)` の複合 FK |
| m5〜m9 | m5 未対応（定数の approver_id）／m6 未対応（承認は workflow に渡らない）／m7 解消（`contracts/apptest.py` に一本化・contract テスト）／m8 未対応／m9 未対応 |
| m10 古い docstring | 未確認（軽微） |

## 新しい所見

### BLOCKER

**R1. release 走査器が migration 0019 を BLOCKED にする（0019 の編集による回帰）。**
0019 に `CREATE TRIGGER ... BEFORE TRUNCATE` を足したため、走査器（`_SQL_DESTRUCTIVE` の `\bTRUNCATE\b`）が 4 文を破壊的と数える（`scan_migration` の実測: destructive SQL line 236/246/256/266）。
回避のために `# contract-migration: approved ADR-0081` を足したが、`domain/release_preflight.py::_check_migration` は承認 ADR を**デプロイ済みの revision**（`c.adr_text(deployed, n)`、=本番の e3c9555）から読む。
ADR-0081 は今回の release で初めて入るので、e3c9555 には存在せず marker は無効になる。結果 `migration_compat` は
`contract migration without a valid approved-ADR marker (ADR deployed, Accepted, names the revision)` で BLOCKED。pre-deploy の必須チェックなので **この head は preflight を通らない**。
- 証拠: `tests/unit/test_review_p3_release_scan_0019.py`（FAIL。実物の 0019 と ADR-0081 を使い、ADR を target 側だけに置く。ADR をデプロイ済み側に置くと同じ 0019 が PASS になることも確認済み）。
- 修正案（どれか）: (1) トリガ文から TRUNCATE の語を走査器に見せない（走査器へ「`CREATE TRIGGER ... BEFORE TRUNCATE` は拒否する側の文」という免除を足し、テストで固定）。(2) ADR-0081 を先に別 release（docs のみ）で入れてから 0019 を出す。(3) TRUNCATE の拒否を migration ではなく別手段にする。(1) が最小。走査器の変更は AGENTS.md §6（設計先行）に当たるので ADR に残す。

### MAJOR

**R2. BGM を ON にしても、説明欄の構成 flag（`AVP_UPLOAD_DESCRIPTION_COMPOSED`、既定 OFF）が OFF だと BGM のクレジットが説明欄に入らない。** `BgmCreditSource.credit_lines`（`infrastructure/music/credits.py`）は flag OFF で `()` を返し、`build_upload_metadata` も composition 無しでは credits を使わない。
`attribution_required` の曲（CC-BY 等）を使うと帰属表示なしで公開されうる。gate の `bgm_quality_checks` が見るのは証跡のクレジット原文で、説明欄ではない。ADR-0086 §5 は「flag OFF では読まない」と書くだけで、BGM と構成 flag の組み合わせを禁じていない。
修正案: `BGM_ENABLED` が ON で構成 flag が OFF のとき、worker 起動を止める（`require_quality_gate` と同じ形のガード）、または `attribution_required` の曲を cue に入れない。

### MINOR

- **R3（解消: claude/p4-flag-wiring）.** 4 flag を読む service にだけ `${FLAG:-false}` で渡す配線（`contracts/release.py::FEATURE_FLAG_SERVICES`、契約テスト `tests/contract/test_compose_feature_flags.py`）と、preflight が `docker compose config` の解決値も見る（`.env` と食い違えば UNKNOWN）対応を入れた。手順は release-runbook 3c。以下は発見時の記述。
- **R3.** `compose.yaml` が container へ渡す新機能 flag は `BGM_ENABLED` だけ（`AVP_QUALITY_GATE_ENFORCE`・`GEO_MAPS_ENABLED`・`AVP_UPLOAD_DESCRIPTION_COMPOSED` は無い。e3c9555 でも同じ）。
  (a) これら 3 つを ON にする手段が runbook に無い（compose の編集が要る）。(b) preflight の `feature_flag_settings` は `.env` しか見ないので、compose の override や container の env で ON にされた flag は検出されない（`enables` の強制はその分だけ穴がある）。
  修正案: preflight は `docker compose config` の解決後の `environment` を読む。flag の受け渡しを compose に明記する。
- **R4（解消。ADR-0086 追補 R4 / INV-149）.** 使用宣言は新しく描いた動画にしか書かれない。ENFORCE を ON にした時点で既に描画済みの動画（同じ入力で再描画は skip）は宣言を持たず、永久に REVIEW（手動承認は可）。自動投稿を待っている既存 Episode が止まる。ENFORCE を ON にする手順に「既存動画の扱い（再描画 or 宣言の補完）」を書く。
- **R5.** preflight の `api_token_file` は `AVP_UID` の既定を 1000 とみなす。`.env` に無く実 UID が違う環境では BLOCKED（安全側。誤検知）。
- **R6.** `FfmpegSpeechMarginProbe` は描画のたびに追加の ffmpeg を走らせる（BGM ON のときだけ）。実測で支障はなかった（下記）が、長尺での時間は未計測。

## flag OFF の安全（変更ごとの再確認）

| 変更 | flag OFF の影響 |
|---|---|
| compose api: bind mount + `AVP_API_TOKENS_PATH` | 隔離の使い捨て project で実験: bind 元ディレクトリが**無くても** container は起動する（compose が空ディレクトリを作る）。token が無いので変更系は 503（fail closed）で、api は落ちない。preflight が事前に BLOCKED にする |
| `AVP_API_TOKENS_FILE` → `AVP_API_TOKENS_PATH` の改名 | 旧名の参照は repo に残っていない（grep 0 件）。まだ誰も設定していないので影響なし |
| 新しい必須 preflight（`api_token_file`・`enables` 必須・flag 照合） | deploy の前提が増える（意図）。`enables: []` の承認ファイルと token ファイルが無いと pre-deploy が通らない。runbook に記載あり |
| 走査器と TRUNCATE の marker | **R1（BLOCKER）** |
| render: `quality_writer` / `speech_probe` | ENFORCE OFF / BGM OFF では `None`（導入前と同一）。起動時 import は問題なし |
| upload: `BgmCreditSource` を常に構築 | 構成 flag OFF では `credit_lines` が DB にも MinIO にも触れず `()`。flag ON でも BGM 証跡が無ければ（`KeyError`）`()` |
| BGM 証跡の key の変更 | BGM はまだ本番で一度も ON になっていない（証跡 0 件）。互換問題なし |
| migration 0019 の編集 | 本番未適用なので編集可（ADR-0086 §6）。expand-only は保たれる（既存の表に触れない）。R1 のみ |

## 実 ffmpeg での話者マージン検証（`scratchpad/bgm-verify-wired/`）

固定版 ffmpeg 7.1.1（sha256 `810f9402…2bf5c`）、本番 Episode `48f4eddb` の export、合成曲（第三者の音源なし）。経路は本番の
`RenderActivities.render_final_video` に **`speech_probe=FfmpegSpeechMarginProbe`・`quality_writer=SqlQualityRecordWriter`・BGM ON** を渡したもの
（`scratchpad/wired_verify.py`。DB は SQLite メモリ、保存先はメモリ ArtifactStore）。その後、実物の `QualityGateService` を
`build_quality_producers(settings, store)` と `BgmCreditSource` つきで構成して自動投稿の判定を実行。

- 証跡に保存された `speech`: 最小マージン **14.80 LU**（`min_margin_centi_lu=1480`）、閾値 12.00 LU、`passed=true`。scene 別（声/BGM/差, LU）: s1 -16.2/-34.2/18.0、s2 -15.6/-36.2/20.6、s3 -16.8/-35.8/19.0、s4 -16.0/-30.8/**14.8**、s5 -17.0/-33.0/16.0。
  ADR-0083 の検証（`bgm_verify_export`、手動で stem を測った値）と一桁目まで一致 = render 内の測定は検証スクリプトと同じ値を出す。
- ラウドネス: 統合 -16.70 LUFS、LRA 14.60 LU、true peak -1.40 dBTP（`bgm_loudness` 合格）。
- 使用宣言: `bgm = used（証跡 ref あり）`、`map = not used`。
- 検査セット: `technical_qa` pass、`description_lint` pass、`bgm`（producer `bgm-quality`）pass（music×2・license×2 すべて pass）。
- gate（自動投稿）: **allowed、verdict pass、reason `upload_allowed`**。`quality_reports` 1 件。
- **fal 呼び出し 0 回**（FalQueueClient / FalStorageClient の全メソッドを spy して失敗させる設定）、`provider_reservations` 0 件。

## テスト結果（ba1e895 + 本レビューの追加）

| 検査 | 結果 |
|---|---|
| `ruff check .` / `ruff format --check .` | All checks passed / 1069 files already formatted |
| `pyright` | 0 errors, 0 warnings |
| `pytest tests/unit tests/contract tests/architecture` | **6391 passed, 1 failed**（6392 件）。唯一の失敗は私の旧レビュー文書が、解消済みで消えたテスト名を参照していた `test_docs_contract`（文書を修正済み。再実行で緑） |
| Temporal replay（CI の 6 ファイル）+ `test_docs_contract` | 90 passed（replay 83 + docs contract 7） |
| 隔離 integration（`run-e2e.sh`、`avp2-oslog-c-p3e`、`-m integration`） | **169 passed, 6 skipped**（rc=0、468 s）。skip 6 件は前回と同じ（`test_logging_collector_lua.py:43` docker が無い）。前回 166 → 169 の増分は wiring / audit の新しい integration（DELETE/TRUNCATE トリガ・FK） |
| 本レビューの追加テスト | `test_review_p3_release_scan_0019.py` 1 **failed（意図。R1）**／`test_review_p3_unwired_quality.py`・`test_review_p3_token_to_approval_e2e.py` は緑 |

---

# Final verification（`d7a3f3f`、Sonnet E）

`claude/v3-phase3-integration` @ `d7a3f3f` を `--no-ff` で merge（衝突なし）。差分 `ba1e895..d7a3f3f` の審査と、オフラインの実 preflight 走査、全スイートを実行した。

## 差分の審査

- **R1 解消**: 0019 から `BEFORE TRUNCATE` のトリガ 4 文と marker を外した（DELETE 拒否のトリガは残す）。TRUNCATE の拒否は「0020 に分ける」と ADR-0081 追補に記載（今回は出荷しない）。
  `tests/unit/test_review_p3_release_scan_0019.py` が緑（実物の 0019 を使う）。副作用として、**TRUNCATE はまだ DB で拒否されない**（m3 の半分は 0020 待ち。UPDATE と DELETE は拒否される）。
- **R2 解消（起動ガード）**: `infrastructure/music/guard.py::require_description_composition_for_bgm` を render と upload の `run_worker` が呼び、`BGM_ENABLED` が ON で `AVP_UPLOAD_DESCRIPTION_COMPOSED` が OFF なら `SystemExit`。テスト `test_bgm_credit_startup_guard.py`。
  残る注意: ガードは各 container の環境変数を読む。`compose.yaml` はこの 3 flag のうち `BGM_ENABLED`（render 側）以外を渡さない（R3）ので、compose を編集して両方を渡さない限り BGM は起動できない = 安全側に倒れる。upload worker には `BGM_ENABLED` が渡らないため、upload 側のガードは compose の編集なしでは働かない（render 側のガードが先に止める）。

## オフライン preflight 走査（本番に触れない）

`RealCollectors(repo)` の git だけを読むメソッド（`migration_files` / `adr_text`）で `_check_migration(Request(target=d7a3f3f), deployed=e3c9555)` を実行:
**`migration_compat PASS — 1 new migration(s), no contract change found`**。`e3c9555` は `d7a3f3f` の祖先（46 commit 先）で `ancestry` の前提も満たす。

pre-deploy の必須チェック全 16 件と、通すために必要なこと（本番の状態は読んでいないので、環境依存のものは「deploy 時に判定」）:

| チェック | 通せるか / 必要な所有者の作業 |
|---|---|
| `target_sha` | 40 桁 SHA を渡す（`d7a3f3f…` の全桁） |
| `target_environment` | deploy 用 worktree の `.env` が本番（staging / `*_test` でない）を指すこと。deploy 時に判定 |
| `worktree_clean` | release worktree が clean（`release/` は gitignore 済み） |
| `ancestry` | **通る**（e3c9555 は祖先）。ff でない場合のみ `--allow-non-ff` が要る |
| `ci` | **要作業**: ブランチを push し、`CI gate` が success になること（この環境の CI はまだ走っていない） |
| `container_revisions` | 本番 container の revision label が e3c9555 であること。deploy 時に判定 |
| `alembic` | 本番 DB が 0018 で、target の head が 0019（expand-only の前進）。deploy 時に判定 |
| `migration_compat` | **通る**（上の実走査） |
| `running_workflows` | 記録のみ（PASS） |
| `provider_reservations` | 未照合の予約が 1 件でもあれば BLOCKED。あれば所有者の waiver（`OWNER-…`）か照合が要る。deploy 時に判定 |
| `schedule_snapshot` | Schedule の状態を記録（deploy 前後で同一であることを事後検査） |
| `service_health` | unhealthy な container が無いこと。deploy 時に判定 |
| `backup_freshness` | **要作業**: `scripts/backup.py run` を実行して 24 時間以内の manifest（DB / MinIO / Temporal）を `--backup-manifest` で渡す |
| `replay_evidence` | **要作業**: `docs/operations/staging.md` §5 の手順で replay 証跡 `release/evidence/<sha>-replay.json` を作る（72 時間以内）。CI 相当の replay テストはこの head で 83 件緑 |
| `api_token_file` | **要作業**: 現時点で `~/.config/avp2/api` は存在しない（BLOCKED になる）。`tokens`（0600、`AVP_UID` 所有、`owner:` と `agent:` の行、各 32 文字以上）を作る。owner を別ファイルにするなら `AVP_API_OWNER_TOKEN_PATH` も |
| `owner_approval` | **要作業**: 承認以外が PASS の preflight 出力を保存 → 所有者が `release/approvals/<sha>.json` を書く。`approval_ref` 必須、`enables` 必須（4 flag OFF なら `[]`）、`report_sha256`。flag を 1 つでも ON にして delegation で承認すると BLOCKED |

## テスト結果（`d7a3f3f` + 本レビューの追加）

| 検査 | 結果 |
|---|---|
| `ruff check .` / `ruff format --check .` | All checks passed / 1072 files already formatted |
| `pyright` | 0 errors, 0 warnings |
| `pytest tests/unit tests/contract tests/architecture` | **6399 passed**（0 failed。463 s。`test_review_p3_*` の 3 ファイルを含み全て緑） |
| Temporal replay（CI の 6 ファイル） | 83 passed |
| 隔離 integration（`run-e2e.sh`、`avp2-oslog-c-p3e`、`-m integration`） | **169 passed, 6 skipped**（rc=0、440 s）。skip 6 件は `test_logging_collector_lua.py:43`（runner に docker が無い）で前回と同じ。スタックは `down` 済み |

## 未解決の所見（最終）

| ID | 重大度 | 内容 |
|---|---|---|
| R3 | 解消 | compose が flag を読む全 service へ `${FLAG:-false}` で渡す（`FEATURE_FLAG_SERVICES`）。preflight は `docker compose config` の解決値も照合する |
| R4 | 解消 | skip 経路で、入力 hash が「BGM・地図なし」を証明する動画にだけ not used を補う（INV-149）。BGM / 地図入りで描画済みの動画は再描画まで REVIEW |
| R5 | MINOR | preflight の `AVP_UID` 既定 1000（誤検知は安全側） |
| R6 | MINOR | 追加の話者マージン測定の所要時間（長尺）は未計測 |
| m3b | MINOR | TRUNCATE はまだ DB で拒否されない（0020 で対応予定） |
| m5 | MINOR | `approver_id` が定数（`api-owner-token`） |
| m6 | MINOR | 手動承認が workflow に渡らない（承認から起動の間に再描画が入ると hash がずれる窓） |
| m8 | MINOR | integration ガードは `os.environ` だけを見る（`.env` は見ない） |
| m9 | MINOR | `quality_reports` が呼び出しごとに増える（上限なし） |
| M2 の限界 | 情報 | owner / agent は同一 OS ユーザーでは分離にならない（ADR-0080 に明記済み） |

BLOCKER・MAJOR は残っていない。

## 最終判定

- **(a) flag すべて OFF の本番 deploy**: **安全。** コード上の互換（起動・replay・`render_input_hash`・upload・migration 0019 の expand-only・課金経路）に問題はなく、`migration_compat` は実走査で PASS。deploy の前に所有者が行う作業は上表の「要作業」（CI、バックアップ、replay 証跡、**API token ファイル**、承認記録に `approval_ref` と `enables: []`）。token ファイルが無いまま deploy すると変更系 API は 503（落ちはしない）。
- **(b) 機能の完成度**: **BGM / 地図 / 品質ゲートは実装として完結**（B1・B2・M1・R1・R2 解消。実素材で話者マージン 14.8 LU・gate PASS・fal 0 回）。ただし本番で ON にするには R3（flag を container に渡す配線と、preflight が実効値を見ること）と R4（既存動画の扱い）の対応が要る。いずれも ON にする判断の前に行う運用/配線の作業で、OFF の deploy を妨げない。
