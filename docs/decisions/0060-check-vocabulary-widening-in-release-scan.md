# ADR-0060: CHECK 語彙の拡張だけを、宣言と機械検査つきで release 走査の contract から外す

## Status

Proposed (2026-10-25)。所有者の承認で Accepted にする。ADR-0050 項目 3 を補う（置き換えない）。

## Context

#10 の migration 0017 は `operational_anomalies.kind` の CHECK に 2 値を足す
（`drop_constraint` + `create_check_constraint`、同じ名前）。PostgreSQL の CHECK は書き換えられず、
旧制約が残る限り新しい値は INSERT できないので、**この形以外に「拡張」は表現できない**。

release preflight（ADR-0050 項目 3）は `drop*` 呼び出しを全て contract とみなし、
`# contract-migration: approved ADR-NNNN` の ADR が **deploy 済み revision に在り** Accepted であることを要求する。
0017 の根拠 ADR-0054 は target にしか無いので、同じリリースでは満たせない。結果 `migration_compat` が
BLOCKED になり、runbook は BLOCKED での deploy を禁じる。さらに ADR-0054 は 0017 を「expand-only」、
ADR-0050 は「CHECK 語彙の拡張も contract」と書いており、文書が食い違っていた。

実際の影響は小さい（本番 DB は 0015、0017 はどの永続環境にも未適用で CI だけ。旧コードが書く値は全て通り続ける。
`operational_anomalies` は約 630 行で、検証の ACCESS EXCLUSIVE は一瞬）。止まっているのは **走査の表現力** であって
データの安全ではない。

## Decision

**CHECK 語彙の拡張だけを、migration の宣言と走査の一致を条件に contract から外す。走査自体は弱めず、
他の `drop*` は従来どおり印が要る。**

1. migration はモジュール直下にリテラルの宣言を置く（0017 の `CHECK_WIDENING`）:
   `table` / `constraint` / `column` / `old`（旧許容値）/ `new`（新許容値）。
2. `scan_migration`（`domain/release_preflight.py`）は、次を **全て** 満たすときだけ、その 1 組の
   `drop_constraint` + `create_check_constraint` を contract と数えない:
   - 宣言が妥当: キーが過不足なく、値は UPPER_CASE の文字列リテラルの tuple（変数・式は不可）で重複が無く、
     **`old` が `new` の真の部分集合**（旧コードの値は 1 つも失われない）
   - `upgrade()`（から呼ばれる関数）に `with op.batch_alter_table("<table>") as b:` があり、本体が
     **ちょうど** `b.drop_constraint("<constraint>", type_="check")` → `b.create_check_constraint("<constraint>", "<column> IN ('V1', 'V2', ...)")`
     の 2 文。名前・表・列は宣言と一致し、IN の値の集合は `new` と一致（リテラルのみ。`OR`・`NOT IN`・`;`・式は不可）
   - 免除は 1 件だけ。同じ宣言でもう 1 回 drop すれば 2 回目は BLOCKED
   不一致・宣言の不正は BLOCKED（無視ではなく destructive の Finding）。ファイル内の他の drop / 破壊的 SQL は
   従来どおり検出する。`downgrade()` は元から走査しない
3. 免除した事実は preflight の `migration_compat` の detail に残す（`CHECK widening (ADR-0060 ...): <制約>: 9 -> 11`）。
   黙って通さない
4. 宣言の `old` が **実際の旧制約と一致する** ことは、preflight（静的）ではなく CI の機械検査が保証する:
   `tests/contract/test_migration_0017_check_widening.py`（prod 形の 0016 から実 INSERT で `old` の全値が通り
   `new` にしか無い値が通らないこと）。`new` が現行コードの `AnomalyKind` と一致することも同じテストで見る
5. 0017 を未リリースのうちに、この形に書き直す（revision id `0017` は変えない。他の migration の `down_revision` を
   壊さないため）。これは本番に入った migration の書き換えではない
6. ADR-0054 の「expand-only」は「CHECK 語彙の拡張（旧値は全て通る）」と読み替える。ADR-0050 項目 3 の
   「CHECK 語彙の拡張（`drop_constraint` + 再作成）も走査上は contract」は、**本 ADR の条件を満たすものを除き**
   従来どおり。runbook の expand / contract 規則も同じ文面に直す

新しい不変条件: INV-82。

### 知っておくこと（0012 の凍結漏れ）

0012 は CHECK を `for k in AnomalyKind` で作る。**新規 DB（CI の SQLite/空の PostgreSQL）は 0012 の時点で最新の
enum 語彙を持ち**、本番は 0012 を旧 enum で適用済みなので旧語彙のまま。つまり 0017 は新規 DB では実質 no-op で、
CI の「upgrade head」だけでは本番の形の遷移は踏めない。本番の形を再現した検査（上の 4、PostgreSQL 版は
`tests/integration/test_migration_against_postgres.py`）が要る。0012 自体は本番に入った migration なので書き換えない
（書き換えは preflight が BLOCKED にする）。今後 enum 由来の CHECK を migration で作るときは語彙を直書きする。

## Alternatives

- **(a) 0017 を直すだけ（走査はそのまま）**: 採らない。PostgreSQL では旧 CHECK を落とさずに語彙を増やせないので、
  どう書いても `drop` が残る。`op.execute("ALTER TABLE ... DROP CONSTRAINT")` は走査の SQL 検出に掛かるうえ、
  走査をすり抜ける書き方を探すのは本末転倒
- **(b) 段階リリース（新しい制約を別名で NOT VALID に足し、旧制約は次のリリースで落とす）**: 採らない。
  両方の CHECK が効くので旧制約が残る限り新しい値は書けず、#10 の機能がそのリリースでは動かない。
  落とす側の migration は結局同じ走査に掛かる（次のリリースでは ADR が deploy 済みなので印で通るが、
  拡張のたびに 2 リリースと手作業の承認が要る）
- **(d) ADR を先に別リリースで出す**: 採らない。内容の無い docs だけのリリースを 1 回挟むことになり、
  拡張のたびに同じ段取りが要る。印の仕組みは「破壊的変更を人が承認した」ことの記録で、旧値が全て通る拡張に
  毎回それを求めるのは過剰。また文書の矛盾（expand / contract）は残る
- **印の仕組みを緩める（deploy 済みでなくてもよい、Proposed でもよい）**: 採らない。ADR-0050 の核心
  （承認が deploy 対象の外にあること）が失われ、他の contract migration にも効いてしまう
- **走査に「同じ名前の drop + create なら通す」を入れる**: 採らない。宣言も値の検査も無いと、
  縮小（旧値を捨てる）や別の制約への差し替えまで通る。本 ADR は旧 ⊂ 新 の宣言と文字列の一致を要求する

## Consequences

- 良い: #10 の `migration_compat` が PASS になる（本番 80775c0 対 #10 で実走確認）。走査は他の contract には
  従来どおり効く（`tests/unit/test_release_check_widening.py` の否定例）
- 良い: CHECK 語彙の拡張が、毎回 ADR を別リリースにする手続きなしで出せる。一方で免除は検査で絞られ、
  PASS の detail に残る
- 悪い（負債）: 走査は引き続き best effort。宣言の `old` が実際の旧制約と一致するかは preflight からは見えない
  （CI のテストが保証。release 時に target の CI が緑であることは `ci` チェックが見る）
- 悪い: migration に宣言と SQL 文字列の両方に語彙を書く重複が生じる。食い違えば BLOCKED になるので片側だけの
  更新は検知される（定義の一元化は走査が「宣言 = 文字列」を強制することで担保）
- 悪い: PostgreSQL の `ADD CONSTRAINT ... CHECK` は既存行を検証する間テーブルをロックする。行数の多い表では
  `NOT VALID` + `VALIDATE` の別手順を使う（その場合は本 ADR の形ではなく contract 扱いで印を取る）
- rollback: コードだけ戻す（`SKIP_MIGRATE=1`）は安全（旧コードの値は通る。新 kind の行は旧コードでは
  `AnomalyKind(...)` で例外になるが watchdog が握りつぶす）。alembic downgrade は新 kind の行が残っていれば
  **拒否する**（2026-10-25 変更: 以前は黙って消していた。監査行を暗黙に失わない。0018 と同じ方針）。消すと決めた所有者が
  行を退避・削除してから再実行する。新 kind の行が無ければ旧 CHECK に戻る
