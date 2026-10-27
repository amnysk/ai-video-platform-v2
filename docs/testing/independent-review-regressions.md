# 独立レビューの回帰テストの理由

`tests/unit/test_review_e_defect_demos.py`

独立レビュー（Agent E、統合 head a83c873）が故障注入で**実証**した欠陥を、修正後もそのまま
回帰テストとして残す。レビュー時は `xfail(strict=True)` で「今は壊れている」ことを示し、
修正ブランチ（F1 / F2）で marker を外した。

- **保留を残した Episode が上限を食い潰さない**（P1-1）: CLI 以外の経路で保留状態を抜けた
  Episode の保留が `MAX_HELD_EPISODES` に数えられ続けると、日次は永久に `HELD_BACKLOG` になる。
- **スキーム無し URL の query を要約に残さない**（P2-5）: 署名付き URL の `sig=` が
  `error_summary` と breaker の evidence に残る。
- **CHECK 拡張の免除で PK/FK/UNIQUE を落とせない・table を作り直せない**（P2-3）: 免除は宣言を
  信じていたので、宣言を偽れば別種の制約の削除や table の再作成が release 走査を通った。

破壊的 SQL の実証（コメント内の `;`、`DO $$ EXECUTE`）は `tests/unit/test_db_destructive_guard.py`
へ移した。理由は docs/testing/db-destructive-guard-rationale.md。
