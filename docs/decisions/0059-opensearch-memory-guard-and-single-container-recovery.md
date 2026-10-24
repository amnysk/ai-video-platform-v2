# ADR-0059: OpenSearch のメモリ保護（hysteresis）・単一コンテナの自動復旧・OpenSearch に依存しない警報

ADR-0040 §7 の「閾値で OpenSearch を止める」運用部分を**更新する**（ADR-0040 の本文は残し、変更点を明記）。

## Status

Proposed (2026-10-25)。所有者の承認待ち。承認までは、コード・設定・文書の変更だけを入れ、本番ホストの
timer / コンテナ / Fluent Bit の設定適用は行わない（`docs/operations/opensearch-recovery.md` の各手順は REQUIRES OWNER APPROVAL）。

## Context

2026-10-21 00:02 JST、`avp-logging-check`（systemd --user timer、5 分毎）の `check-pipeline.sh --enforce-memory` が
MemAvailable の**1 サンプル**（1039MiB < 1.5GiB）で avp2-logging の OpenSearch を `docker stop` した。5 分後には
5487MiB に戻っていたが、起こす仕組みが無く **4 日間停止**。その間:

- 同 script は毎回 exit 1（unit failed）だったが通知の経路が無く誰も気付かなかった（約 960 回）。
- Fluent Bit は OpenSearch へ送れず、`retry_limit: 72`（約 2h49m）で avp_app の chunk を**105 個破棄**した。
  temporal-1 の 10-20T15:02Z から 10-24T13:41Z は json-file の rotation（20MB x 3）で復元不能。
- ADR-0040 §7 の「長く止めるときは Fluent Bit も止める」は手順書の中だけで、実行する主体が無かった。

ホストは swap が満杯で、一時的な MemAvailable の低下は起こりうる（agent の test 実行等）。保護の目的
（OpenSearch がアプリの worker より先に退くこと）自体は正しいので、**目的を残して過敏さと一方通行を直す**。

## Decision

1. **停止に hysteresis**（ADR-0040 §7 の変更）: MemAvailable が 1.5GiB 未満の**連続 5 サンプルかつ 180 秒以上**
   で OpenSearch（と Dashboards）を止める。Dashboards は 2GiB 未満で同様。サンプル間が 150 秒を超えたら
   連続とみなさない（timer が止まっていた間の古い値で動かない）。1 回 = 1 サンプルの `scripts/opensearch-guard.py` を
   30 秒毎の systemd --user timer から動かす。`check-pipeline.sh --enforce-memory` は**廃止**（受け付けるが何も止めない）。
2. **状態を残す**（`guard-state.json`）: 停止の理由・`stopped_at`・直近のサンプル・復旧条件・24 時間の起動試行。
3. **安全な自動復旧は opt-in（`--auto-restart`）**: guard が止めた OpenSearch **だけ**を `docker start <その 1 コンテナ>`
   で起こす。条件は全て: MemAvailable >= 4096MiB の連続 3 サンプル / avp2 の全コンテナが healthy（正常終了した
   one-shot を除く）/ 停止から 10 分以上 / 直近の試行から `600s x 2^過去の試行回数` 以上 / 24 時間の試行が
   3 回未満。上限に達したら**止めたまま** crit 警報（RESTART_CAPPED）。compose up・recreate・rm・volume には
   触れない。guard が止めていない停止（人・旧 timer）は起こさず警報だけ（OPENSEARCH_DOWN_UNMANAGED）。
   復旧線 4096MiB は停止線 1.5GiB より明確に高く（mem_limit 2.5GiB + 停止線の余裕）、起動直後の再停止ループを避ける。
4. **警報は OpenSearch・Temporal・DB に依存しない**: `scripts/avp_alerts.py`（journal 向けの `<N>avp-alert` 行・
   `alerts.jsonl`・`alerts-current.json`）。出す警報: OpenSearch 停止 / 停止が 30 分超 / 復旧上限 / Fluent Bit 未稼働 /
   buffer が上限の 70%・90% / 最古の chunk が retry 窓の 50%・80% / chunk の破棄。
   既存の通知口（ADR-0027 の `LoggingAnomalyNotifier`）はコンテナ内の ERROR ログ + DB 行で、OpenSearch が落ちた
   ときのログ基盤の警報には使えない（ログ自体が見えない）。Temporal の watchdog はコンテナの中で動き、ホストの
   state dir を読めない（mount の追加は本番 compose の変更で別判断）ので、読む側は §5 の独立した timer とした。
5. **dead-man's switch**: guard と host-process-monitor（PID/zombie）は毎回 `heartbeat-<component>.json` を書く。
   **別の unit・別の timer** の `scripts/monitor-watch.py` が古い・無い heartbeat を crit にする（5 分毎）。
   host-process-monitor は同じ heartbeat・警報口へ統合する。
6. **Fluent Bit**: 停止時の取りこぼし対策として **`retry_limit` を 72 から 288（約 11 時間）へ上げる**（設定の変更だけ。
   適用は Fluent Bit の再起動で所有者承認）。無限（`no_limits`）にしないのは、永久に 400 になる chunk（実例:
   `_id` 512 bytes 超）が buffer を埋めるため。窓を伸ばしても有限なので、警報（buffer・chunk 年齢・破棄）を併用する。
   長期停止で Fluent Bit 自体を止める判断は人間（runbook §4）。guard は Fluent Bit を止めない（触るコンテナを増やさない）。
7. **取り戻せる範囲の可視化**: `scripts/log-recoverability.py`（読み取り専用）が、停止窓についてコンテナごとに
   recoverable / unrecoverable の範囲を出し、窓内の行を backfill 用に複写する。再投入は所有者承認。

### ADR-0040 §7 の変更点（旧テキストは ADR-0040 に残す）

| ADR-0040 §7（旧） | 本 ADR（新） |
|---|---|
| MemAvailable が 2GiB を下回ったら Dashboards を、1.5GiB を下回ったら OpenSearch を止める（1 サンプル） | 連続 5 サンプルかつ 180 秒以上。サンプル間 150 秒超は連続でない |
| systemd timer から `check-pipeline.sh --enforce-memory` | `opensearch-guard.py`（30 秒毎）。`--enforce-memory` は廃止 |
| 復旧の定め無し（手動） | `--auto-restart`（opt-in）の単一コンテナ起動、または runbook の手動手順 |
| 長く止めるときは Fluent Bit も止める（手順書のみ） | retry_limit 72 -> 288、警報、判断は人間 |

### 不変条件

- **INV-79** メモリ逼迫での OpenSearch 停止は一時的な低下では起きず、復旧は guard が止めた OpenSearch 1 コンテナだけを、条件が揃ったときだけ行う
- **INV-80** ログ基盤・ホスト監視の警報と heartbeat は OpenSearch に依存せず、監視が止まったことを別の timer が検知する
- **INV-81** ログ収集の欠損（buffer 上限・retry 窓の接近・破棄）は破棄の前後で警報になり、取り戻せる範囲の調査は読み取り専用

## Alternatives

- **retry_limit を `no_limits` にする**: 破棄は無くなるが、永久に拒否される chunk が buffer を埋め、上限（1GiB）で古い
  chunk から無差別に破棄される。採らない（有限の 288 + 警報）。
- **メモリ逼迫時に Fluent Bit も guard が止める**: 位置 DB から再開できるが、止めるコンテナと復旧の組合せが増え、
  json-file の rotation（20MB x 3）より長く止めると別の欠損になる。判断は警報を見た人間に残す。
- **`docker compose up -d` で復旧**: 設定の再評価で他のコンテナが再作成されうる。`docker start` の単一コンテナだけにする。
- **systemd の `Restart=` / OOM 設定だけに任せる**: OpenSearch は `docker stop` で止まった（OOM kill ではない）ので効かない。
- **Temporal watchdog に host の heartbeat を読ませる**: コンテナにホストの dir を mount する本番 compose 変更が要る。
  独立した timer の方が、Temporal・DB・OpenSearch のどれが死んでも動く。将来の統合は別 ADR。

## Consequences

- 一時的なメモリ低下で OpenSearch は止まらない。持続的な逼迫では従来どおり止まり、crit 警報が journal に出る。
- `--auto-restart` を入れない限り復旧は手動（runbook）。入れても上限 3 回/24h で止まる。
- Fluent Bit が OpenSearch に送れない時間の許容は約 11 時間（旧 約 2h49m）。それ以上は警報を見て人間が判断。
- 導入物（timer 2 本・コピー先 `~/.local/lib/avp-host/`）はすべて所有者承認後（runbook §7）。
- 通知先（メール・チャット等の人間の目に届く経路）は未整備。警報は journal・ファイル・`monitor-watch.py --show-alerts`・
  `systemctl --user --failed` まで。人に届ける経路は別途決める必要がある（未決事項）。

### grep 結果（AGENTS §8: 新しい設定キーの分類）

`grep -rn "enforce-memory" apps/ workers/ domain/ infrastructure/ contracts/ docs/ deploy/ scripts/ tests/`:
読み手 1 ファイル（check-pipeline.sh。廃止の警告だけ。3 行）/ 書き手 0（リポジトリの unit からは外した。
本番に置いた旧 unit のコピーは repo 外で、所有者が §7.4 で外す）/ 説明 5 ファイル（ADR-0040・platform.md・
logging-runbook.md・host-process-monitor.md・本 ADR。前三者は本 ADR で更新済み）。`retry_limit` は fluent-bit.yaml 2 + 契約テスト 1 + guard の定数 1
（`test_guard_constants_match_the_fluent_bit_config_and_documented_conditions` が一致を強制）。
