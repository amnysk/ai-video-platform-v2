# ADR-0050: 読み取り専用の release preflight と、本番から隔離した staging

## Status

Proposed (2026-10-20)。所有者の承認で Accepted にする。

## Context

本番の deploy は `scripts/deploy-workers.sh`（`make deploy-workers`）が build → migrate → 全サービス再作成までを行う
（ADR-0024 追補・ADR-0027）。しかし「この SHA を出してよいか」の判断は人の記憶と手作業に依存している。
過去に起きた/起こり得る事故:

- 2026-09-20: 稼働中の worker が3つの commit で混在していた（`docs/testing/worker-versions.md`）
- 2026-09-19: deploy の pause が解除されないまま翌日を迎えた（ADR-0027）
- migration が破壊的（列削除・NOT NULL 追加）でも、旧コードのコンテナが残っている間に流れうる。
  alembic の downgrade は前方の行を失うので、戻す手段として当てにできない
- provider 予約（ADR-0013）が `reserved` のまま code を入れ替えると、再送（二重課金）の判断材料を失う
- 有料 API・YouTube に触れずに migration や UI を試せる場所が無い（INV-18 / AGENTS.md §9）

## Decision

**deploy の前後に、何も変更しない三値（READY / BLOCKED / UNKNOWN）のチェックを走らせ、
UNKNOWN を READY として扱わない。staging は本番と何も共有しない別 compose project にする。**

1. `scripts/release-preflight.py` は純粋な判定（`domain/release_preflight.py`）と、注入される collector
   （`infrastructure/release/`）に分かれる。collector は git / docker inspect / SELECT / Temporal の
   describe・list / `gh api`（GET）/ ファイルの読み取りだけを行う。書き込み・再起動・Schedule 変更・
   有料 API・YouTube は呼ばない
2. 各チェックは PASS / BLOCKED / UNKNOWN。総合は BLOCKED > UNKNOWN > READY の優先で、
   **必須チェックが1つでも欠ければ UNKNOWN**。READY は必須が全て PASS のときだけ。
   終了コード READY=0 / BLOCKED=2 / UNKNOWN=3
3. migration は expand-only を既定とする。新しい migration に `drop_column` / `drop_table` / rename /
   `server_default` の無い `nullable=False` の追加があれば BLOCKED。`# contract-migration: approved ADR-NNNN`
   の印が（その ADR が存在する形で）あるときだけ通る
4. rollback は「前の SHA のイメージで再 deploy」。alembic downgrade に依存しない。contract migration が
   既に流れた後は、バックアップからの復旧＋所有者の判断（自動化しない）
5. `--verify-post-deploy` は deploy 後の読み取り専用検査（全コンテナ revision == target、必須 task queue に
   poller、Schedule の状態が preflight 時点の記録と同じ、alembic == head）
6. staging は `compose.staging.yaml`（project `avp2-staging`）。DB・bucket・volume・port・Temporal namespace を
   本番と共有せず、FAL / YouTube の資格情報は空。契約テストが compose.yaml との非衝突を検査する

新しい不変条件: INV-65。deploy-workers.sh / with-maintenance-pause.sh は変更しない（preflight は前後に足すだけ）。

## Alternatives

- **deploy-workers.sh に検査を組み込む**: 採らない。deploy スクリプトは本番を変更する経路で、そこに「判定の
  UNKNOWN 扱い」を混ぜると判定の変更が本番手順の変更になる。判定は読み取り専用の別物にする
- **二値（OK/NG）**: 採らない。「確認できなかった」を OK に丸めると、gh や Temporal が落ちている日に
  素通りする。production-preflight（ADR-0030）と同じく未検証を区別する
- **downgrade を rollback 手段にする**: 採らない。前方に書かれた行を落とす/破壊的 downgrade は
  データ損失を生む。expand/contract で「旧コードが新スキーマで動く」ことを保証するほうが安い
- **staging を compose.yaml の profile にする**: 採らない。同じファイルだと volume 名・port・bind path の
  共有が1行の誤りで起きる。別ファイル＋契約テストで衝突を機械的に検出する
- **CI から本番へ自動 deploy**: 採らない（AGENTS.md §9: 所有者の明示判断）

## Consequences

- 良い: 「出してよいか」が再現可能になり、確認できなかった項目が UNKNOWN として見える。
  staging で migration を本番の dump 復元コピーに対して予行できる
- 悪い（負債）: 必須の証跡（バックアップ manifest・replay 証跡・承認ファイル）は人が作る。
  作り忘れると UNKNOWN で止まる（意図した挙動）。migration の静的走査は文字列ベースで、
  `op.execute` の生 SQL は `DROP TABLE/COLUMN`・`RENAME`・`TRUNCATE`・`SET NOT NULL` の語だけを検出する（それ以外の破壊的 SQL は見逃しうる）
- 悪い: staging は本番と同一ハードウェア上で動かすと負荷を奪う。起動は所有者の判断。
  staging は fal / YouTube を呼べないので、provider を通る経路は fake でしか検証できない
- 悪い: CI の check-runs は「全て success」しか見ない。必須チェック名の固定は未実装
