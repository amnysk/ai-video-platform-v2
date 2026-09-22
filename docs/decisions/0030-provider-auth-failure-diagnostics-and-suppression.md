# ADR-0030: provider 準備呼び出しの失敗診断・分類の是正と共有障害の抑止

## Status

Accepted (2026-09-23)

## Context

2026-09-22 06:20:58、Episode `8fb66fcb-a098-4e44-a9cb-9d845622b5f7` の sb6 動画生成準備で
`FalStorageClient._token`（`infrastructure/providers/fal_storage.py`）が HTTP 403 を返した。
sb1〜sb5 は成功していた。Production は `needs_input` に分類され Episode は `blocked` になり、
Render・Upload には未到達だった。

調査で判明した事実（`.worktrees/claude-daily-hardening` commit `4d96027`）:

1. `_raise_for_status`（`fal_storage.py:40-48`）は 401/403 を無条件に
   `ProviderUnavailableError(f"fal storage {what} refused: HTTP {status} (credentials)")` にしている。
   **`(credentials)` は自作コードの決め打ちラベルであり、fal の応答本文から得た事実ではない。**
   失敗時の応答本文はどこにも記録・検査していない。
2. `_token` はキャッシュを持たず、`upload()` のたびに毎回新規に取得する
   （`fal_storage.py:90-122`）。したがってプロセス内のトークン使い回しによる期限切れ・不整合は
   原因から除外できる。
3. このストレージ・アップロード準備（`FalSeedanceVideoGenerator.prepare()`,
   `fal_seedance_video.py:112-129`）は `PaidJobRunner.submit()`
   （`infrastructure/production/paid_job.py`）内で**予約 INSERT より前**に呼ばれる
   （ADR-0013/0017 の順序どおり）。したがって sb6 の 403 では**予約も課金も一切発生していない**。
4. `fal_storage.py` には診断ログが一切無い（`logging.getLogger` の宣言も無い）。操作種別・HTTP
   status・provider request id・worker識別子・設定版・発生時刻のいずれも構造化して残らない。
5. `FAL_KEY` は worker プロセス起動時に一度だけ読み、プロセス寿命の間使い回す
   （`workers/production_video/run_worker.py`）。video の並行数は 1（`video_concurrency=1`、逐次実行）。
   sb5 と sb6 の間で fal 側のキー状態が変化した（レート制限・一時的な認可基盤の不調・実際の失効等）
   ことと矛盾しない。
6. 調査時点で、Infra の起動元 `.worktrees/claude-production-autopilot` は `claude/daily-hardening`
   の祖先である `claude/topic-planner`（`e29c7e8`）を指しており、かつ `fal_seedance_video.py` /
   `infrastructure/youtube/oauth.py` を含む未コミット差分を持っていた。この worktree は他エージェントの
   進行中作業であり、本 ADR の作業からは**読み取り専用**で参照するに留め、書き込み・統合はしない。
   したがって「実際に本番へデプロイされていたコードが `claude/daily-hardening` 4d96027 と完全に
   一致していたか」は本調査だけでは確定できない。

6. `docs/testing/worker-versions.md`（2026-09-20 15:00 JST 実測）は、稼働中コンテナが
   最大3種類の異なる commit を混在させて動いていたことを既に記録している
   （production-image/production-video は `f38835c`、production-voice/render/upload は
   `5a1a0cd`、api は `8f55bc4` で、いずれも `daily-hardening` 先端と不一致）。事故当日
   （2026-09-22）にこの混在が解消していたかは本調査だけでは確認できない。worker 間で
   `FAL_KEY` の値自体が食い違うことは無い（同じ compose 環境変数を全 production worker が
   参照する）が、混在した commit のどれが `fal_storage.py` の実際の挙動だったかは、
   version 混在が解消済みと確認できない限り不確かである。

**403 の確定原因は本調査では特定できない。** 理由は (a) 発生時点の応答本文・ヘッダが保存されておらず
事後に取得しようがないこと、(b) インフラ起動元と worker 起動元のコミットが食い違っていた可能性を
排除できないこと、の2点。本 ADR が閉じるのは「原因不明でも次に同じ状況が起きたときに
証拠が残り、誤った確信（`(credentials)`）で自動的に決め打たない」という**プロセスの欠陥**であり、
2026-09-22 の 403 そのものの原因確定ではない。

## Decision

**(1) 診断情報を構造化してログに残す。** `fal_storage.py` の非 2xx 応答（`token` / `upload` 双方）で、
`PROVIDER_AUTH_FAILURE`（または5xx/timeout系は `PROVIDER_TRANSIENT_FAILURE`）という ERROR ログを
`fal_operation` / `http_status` / `provider_request_id`（応答ヘッダの `x-fal-request-id` 等、fal 公式
ドキュメント/SDK で確認できた実在のヘッダ名を使う。無ければ `null`）/ `worker_id`（hostname
または task queue 名 + プロセス識別子）/ `config_version`（`scripts/workers-versions.sh` が使う
リビジョン表現を再利用。新設しない）/ `occurred_at` の各フィールドで出す。
**Authorization ヘッダ・token 値・応答本文の生データは出さない**（INV-20）。応答本文は、
秘密が混入しない保証ができない限り記録しない。

**(2) 分類そのものは変えず、文言と証拠だけを是正する。** 実装を読み直した結果、
接続エラー・timeout・5xx・429 は既に `ProviderInvocationError`（`RetryableError`）に分類されており、
`workers/production/workflows.py::_rounds` の submit 分岐（`retryable = cls in RETRYABLE_FAILURE_CLASSES；
not retryable or attempt >= budget` でだけ `_StageFailure` を送出）が、この分類を使って
**自動的に次のラウンド（新しい submit）へ進む**（`production_video_max_rounds` の予算内）。
これは INV-15 の「submit は Temporal に retry させない・retry はラウンド」と整合しており、
是正の必要は無かった（`tests/unit/test_fal_storage.py` の既存テストがこの分類を機械検査していた）。
変えるのは次の2点だけ:

| 状況 | 分類（変更なし） | 変えたこと |
|---|---|---|
| 接続エラー・429・5xx | `retryable`（`ProviderInvocationError`） | `PROVIDER_TRANSIENT_FAILURE` 診断ログを追加 |
| その他 4xx | `needs_input`（`ProviderRejectedError`） | `PROVIDER_REJECTED` 診断ログを追加 |
| 401/403 | `needs_input`（`ProviderUnavailableError`） | 文言から `(credentials)` の断定を外し「HTTP {status} で
  拒否された。原因は `PROVIDER_AUTH_FAILURE` 診断ログを参照」に変える。診断ログを追加 |

`(1)` のログとセットでなければ、401/403 の文言修正だけでは前と同じ「グレップで確定」の悪癖を
繰り返すだけなので、両方を同じコミットで入れる。

**(3) 本番と同じ経路の preflight を作る。** `scripts/production-preflight.py` を新設し、
worker と同じ `infrastructure/config.py` の設定解決と同じ `FalStorageClient` 構築コードを
**import して使う**（設定を再実装しない）。fal の token 取得エンドポイントが非課金であることを
fal 公式ドキュメント/SDK で確認できた場合に限り実呼び出しで検証する。確認できない場合は
実呼び出しを行わず「未検証」と明示する。出力は「検証済み」と「未検証」を明確に分け、
未検証の項目を healthy として扱わない。環境変数の存在確認だけでは healthy と判定しない。

**(4) 共有の資格情報障害を検出し、新規課金を抑止する。** 新テーブル `provider_auth_incidents`
（`provider` / `http_status` / `episode_id`（nullable）/ `occurred_at` / `resolved_at`）を追加する。
`fal_storage.py` の 401/403 到達時に1行記録する。`PaidJobRunner.submit()` は予約 INSERT の**前**に
「同じ provider に対して直近 `AUTH_INCIDENT_WINDOW_MINUTES`（既定10分、`contracts/` に宣言）以内、
未解決の `provider_auth_incidents` が `AUTH_INCIDENT_SUPPRESSION_THRESHOLD`（既定3件、同宣言元）件
以上あるか」を確認し、超えていれば `ProviderCredentialSuspectedOutageError`（needs_input）で
**予約を作らずに**止める。これにより新規課金は発生しない。別 provider・別 Activity は影響を受けない
（provider 単位でのみ絞る）。回復判定: 同じ provider への呼び出しが成功したら、その provider の未解決
incident を `resolved_at` で閉じる（自動）。ウィンドウを過ぎた古い incident は次の判定に数えない
（時間経過でも実質的に解除される）。手動での強制解除は運用者が DB を直接見て判断する
（自動の無条件解除は仕込まない。INV-15 と同じ「自動で解放しない」思想）。

`operational_anomalies`（日次1行）は本テーブルの目的（数分単位のバースト検出）に対して粒度が粗すぎるため
流用しない。既存の watchdog 通知経路とは別レイヤ（submit 前のゲート）であり、ADR-0031 の
watchdog 拡張とは独立に働く。

## Alternatives

**(a) 401/403 を維持しつつ preflight だけ足す** — 事後対応は改善するが、次の事故でも同じ
「グレップで確定」を繰り返す。却下。

**(b) `provider_auth_incidents` を作らず `operational_anomalies` を再利用する** — テーブルが増えない。
しかし `(kind, anomaly_date)` の一意制約は1日1行しか許さず、数分単位のバースト検出に使うと
既存の日次異常検知の意味論を壊す。却下。

**(c) 資格情報の失敗を検出したら該当 provider の Activity を完全に無効化する（worker 停止）** —
確実だが、無関係な Episode・シーンまで止め、INV-13（1つの失敗が他を止めない）に反する粒度になる。
provider 単位の submit ゲートに留める。却下。

**(d) 秘密管理サービスを新設し、そこでトークンの healthy 判定を持つ** — ユーザーの明示的指示で不要。却下。

## Consequences

**良い側**
- 次に 403 が起きたとき、応答由来の事実（status・request id・worker・設定版）が残り、
  「credentials と決め打ち」を防げる
- preflight が本番と同じ経路を通るので「環境変数はある」の空検査で healthy と誤判定しない
- 共有障害時に新規課金を止めつつ、無関係な provider・Episode は動き続ける

**悪い側 / 引き受けた負債**
- 2026-09-22 の 403 の確定原因は依然として不明のまま（§Context）。本 ADR はプロセスの欠陥を閉じるが
  過去の事故そのものは未解明
- `provider_auth_incidents` の自動解除は「時間経過」と「次の成功」だけに依存する。fal 側の障害が
  ウィンドウをまたいで断続的に起きると、抑止と解除を行き来しうる（安全側に倒れているだけで万能ではない）
- provider request id が fal の応答に無い場合は `null` のまま。fal 側の運用ログとの突合はできない
- preflight の「非課金であることの確認」は本 ADR 作成時点の fal 公式資料に基づく。fal 側の仕様変更で
  課金対象になった場合、preflight 自体が想定外の課金を発生させうる（陳腐化条件に明記）

## 陳腐化条件

- fal の token/upload エンドポイントの課金方針が変わったとき（preflight の非課金前提の再検証が必要）
- fal が request id をレスポンスヘッダ以外の形で返すようになったとき

## 機械検査

- `tests/unit/test_fal_storage.py`（新規: 401/403/429/5xx の分類、`(credentials)` 断定文言の削除、
  診断ログのフィールド、secret が出ないこと）
- `tests/unit/test_paid_job.py`（新規: 未解決 incident 件数超過で予約が作られず needs_input になること、
  他 provider は影響を受けないこと、成功で incident が解決されること）
- `tests/unit/test_production_preflight.py`（新規: 「検証済み」と「未検証」を混同しないこと、
  secret が出ないこと）
- `tests/contract/test_migration_matches_models.py`（既存・汎用: `provider_auth_incidents` を含む
  全テーブルでマイグレーションとモデル定義が一致すること）
- `tests/architecture/test_no_live_calls.py::test_only_sanctioned_modules_import_fal_adapters`
  （既存: fal adapter を import してよい場所の allowlist に `infrastructure/production/preflight.py`
  を追加）
