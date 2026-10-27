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
3. migration は expand-only を既定とする。新しい migration の `upgrade()`（と、そこから同じファイル内で
   呼ばれる関数）に `drop*` 呼び出し・rename・列の型変更・`server_default`（None でない値）の無い NOT NULL
   （`nullable=False` / `primary_key=True`）・破壊的 SQL（`DROP` / `DELETE FROM` / `RENAME` / `TRUNCATE` /
   `SET NOT NULL` / 型変更 / DEFAULT 無しの `ADD ... NOT NULL`）があれば BLOCKED。静的に読めない書き方
   （非リテラルの `op.execute`、`nullable=<変数>`、`**kwargs`、列を変数で渡す `add_column`）は UNKNOWN。
   本番に入った migration ファイルの書き換え・削除は BLOCKED（内容 hash 比較）。
   `# contract-migration: approved ADR-NNNN` は **コメント token** にあり、その ADR が **deploy 済み revision**
   に存在し、`## Status` が Accepted で、その migration の revision id に言及しているときだけ通る
   （文字列・docstring 内の同じ文面、target にだけ在る ADR、Proposed の ADR では通らない）。
   **best effort**: 静的走査は抜けうる。通過は安全の証明ではなく、人のレビューは省けない。
   CHECK 語彙の拡張（`drop_constraint` + 再作成）も走査上は contract なので、印が要る
   （例外: ADR-0060 の `CHECK_WIDENING` 宣言と 2 文が一致し、旧集合が新集合の真の部分集合である 1 件だけは contract と
   数えない。それ以外は従来どおり）
4. rollback は「前の SHA のイメージで再 deploy」。alembic downgrade に依存しない。contract migration が
   既に流れた後は、バックアップからの復旧＋所有者の判断（自動化しない）。
   旧イメージの `migrate`（`alembic upgrade head`）は DB が先にいると `Can't locate revision` で失敗するので、
   rollback は **migrate を走らせない**。`scripts/release-preflight.py --rollback` は、target の head から本番 revision までの
   migration（走っているコードのツリーから読む）が全て expand-only で、かつ承認記録に
   `rollback.old_code_compatible: true` と `rollback.revisions`（先に進んだ revision の全て）があるときだけ
   `alembic` を PASS にする（記録が無ければ UNKNOWN、contract を含む/revision が違えば BLOCKED）。
   `--rollback` の事後検証は「DB が preflight 時点の revision のまま」を見る。
   **deploy-workers.sh に、既定オフの `SKIP_MIGRATE=1` だけを足す**（migrate を走らせず migrate コンテナを削除し、
   残りを作り直す。0/1 以外は拒否。`tests/unit/test_deploy_scripts.py`）。それ以外の deploy の挙動は変えない
5b. `container_revisions` / `containers_at_target` は、`deploy-workers.sh` の `APP_SERVICES`（one-shot の migrate を除く。
   `compose.yaml` の `restart: "no"`）が全て走っていることも見る（欠けていれば BLOCKED、一覧が空/読めなければ UNKNOWN）。
   定義は `infrastructure/release/app_services.py` が既存のものから導く
5a. 承認・証跡は自己申告で、**認証ではなく取り違えの防止**。承認記録は SHA・`approved_at`（TZ 付き。未来日付と
   既定 72 時間（上限 168）超は BLOCKED）・`report_sha256`（owner が読んだ preflight の `--json` 出力の sha256。
   `--approved-report` で照合し、その出力は承認以外が全て PASS で SHA と mode が一致）に束縛する。replay 証跡も
   `at` で鮮度を見る。事後検証は `--pre-report` が承認の `report_sha256` と同一であること（`approval_binding`）と、
   mode/SHA/総合 != BLOCKED を確認する。署名した git tag を承認とする等の強い手段は所有者の判断
5. `--verify-post-deploy` は deploy 後の読み取り専用検査（全コンテナ revision == target、必須 task queue に
   poller、Schedule の状態が preflight 時点の記録と同じ、alembic == head）
5b. 全ての判定の前に `target_environment`（必須）が、読みに行く DB / Temporal の宛先を確認する: namespace が
   `avp-staging`、`AVP_ENVIRONMENT` に staging、DB 名が `_test` 終わり/`avp_staging` なら BLOCKED。DB・Temporal を
   読むチェックの detail には宛先（DB の host/名前・namespace。資格情報は出さない）を併記する。設定は cwd ではなく
   repo root の `.env` を読む。識別子の宣言元は `contracts/release.py`
6. staging は `compose.staging.yaml`（project `avp2-staging`）。DB・bucket・volume・port・Temporal namespace を
   本番と共有せず、FAL / YouTube の資格情報は空。契約テストが compose.yaml との非衝突を検査する

新しい不変条件: INV-65。preflight は deploy を包まず前後に足すだけ。deploy-workers.sh の変更は rollback 用の既定オフの
`SKIP_MIGRATE=1` のみ（上の 4）。with-maintenance-pause.sh は変更しない。使い方の誤り・想定外の失敗は終了コード 3
（argparse の既定 2 は BLOCKED と衝突するため）。

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
  作り忘れると UNKNOWN で止まる（意図した挙動）。migration の静的走査は best effort で、
  AST と SQL の語の検出に依る（見逃しうる。人のレビューは必須）
- 悪い: staging は本番と同一ハードウェア上で動かすと負荷を奪う。起動は所有者の判断。
  staging は fal / YouTube を呼べないので、provider を通る経路は fake でしか検証できない
- CI 判定は集約 job `CI gate`（名前の唯一の宣言元は `contracts/ci.py`）の check run が success であることだけを見る。
  gate が無ければ UNKNOWN、gate が失敗なら BLOCKED。同じ SHA に複数の gate run（push と PR）があれば、1つでも失敗なら BLOCKED
