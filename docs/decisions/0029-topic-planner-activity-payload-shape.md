# ADR-0029: Topic Planner の Activity 境界は「型注釈どおりの形」だけを返す

## Status

Accepted (2026-09-20)

## Context

2026-09-20 の実 provider E2E は、最初の工程（Topic Planner）で止まった。`topic_gather_context` の結果
（`PlanningContext`）を workflow が復号できず、workflow task が失敗し続けた:

```
RuntimeError: Failed decoding arguments
  TypeError: Failed converting field analytics on dataclass PlanningContext
  … field totals on dataclass AnalyticsSummary
  … Failed converting value for key 'content_types' in mapping dict[str, float]
  … Expected value to be int|float, was <class 'dict'>
```

原因は `workers/planning/topic_activities.py::_totals` が `totals["audience"] = asdict(report.audience)` を入れていたこと。
`AudienceShares` は `age_groups` / `genders` / `countries` / `content_types`（`dict[str, float]`）を持つので、
`totals["audience"]` は「指標 → 数値」ではなく「指標 → dict」になり、
`AnalyticsSummary.totals: dict[str, dict[str, float]]`（窓 → 指標 → 数値）の型注釈と食い違う。
Temporal の既定 converter は型注釈で復号するので、Activity 自身は成功し（送信側は検査しない）、
復号する workflow 側だけが失敗する。

**なぜ今まで出なかったか**: 実 Analytics だけが `AudienceShares` の内訳を返す。単体テストの fake は
`AudienceShares(country_us=…, age_18_24=…)` の scalar だけで、内訳を返さなかった。
`yt-analytics.readonly` scope が付いた（2026-09-19）まで、実 API 由来の内訳は Planner に届いていない。
このままなら**毎日 06:00 の Daily も同じ箇所で止まった**。

もう一つの問題: 復号の失敗は Activity の例外ではなく workflow task の失敗なので、Temporal は成功するまで
無限に再試行し、失敗として終わらない。ADR-0025 の fallback の梯子（live → snapshot → 無し）は Activity の
例外しか捕まえないため、この失敗クラスには届かなかった。watchdog（ADR-0027）も「起動しなかった」ことしか見ない。

## Decision

1. **`totals` は窓だけ**（`"7d"` / `"28d"` / `"90d"` → 指標 → 数値）。視聴者構成は別フィールド
   `AnalyticsSummary.audience: dict[str, dict[str, float]]` に置く（`summary` = country_us など単一の比率、
   それ以外は `AudienceShares` の dict フィールド名の下に内訳）。項目名は `AudienceShares` のフィールドから導き、
   列挙し直さない（AGENTS.md §8）。新フィールドは既定値ありなので、履歴中の古い結果も復号できる。
2. **返す直前の往復検査**（`ensure_decodable`）: `gather_context` は、workflow が使うのと同じ経路（既定 converter、
   型注釈で復号）で `PlanningContext` を往復させ、復号できなければ analytics を捨てて `no_analytics` に劣化させる。
   警告ログには例外の型名だけを書く（値・例外文は写さない / INV-20）。Content Memory 側が原因なら劣化しても復号できず、
   握りつぶさず例外にする。
3. **保存済み snapshot が読めない形**（別版の項目など）は無視して次の段（stale → 無し）へ進む。Activity が例外を
   出し続けて Planner が止まることを避ける（analytics は best-effort）。
4. **境界の型ごとの往復テスト**: Topic Planner の契約の dataclass すべてを、実際の形（非空の入れ子）で
   既定 converter に往復させる。契約に型を足したときの漏れも検査する
   （`tests/unit/test_topic_planner_payloads.py`）。加えて worker 経由（time-skipping Temporal）で、内訳つきの実形状の
   Analytics が prompt まで届くことを固定する（`test_topic_planner_workflow.py`）。

## grep（AGENTS.md §8: `totals` / `audience` の読み手・書き手）

`grep -rn "totals\|audience" apps/ workers/ domain/ infrastructure/ contracts/ docs/`（`audience_*` のプロファイル項目と
`audience_fit` を除く）:

| 分類 | 件数 | 場所 |
|---|---|---|
| 書き手 | 2 | `topic_activities._totals`（窓の合計のみ）、`topic_activities._audience`（新規） |
| 読み手 | 1 | `generate_candidates` が `asdict(ctx.analytics)` を prompt の JSON に載せる（構造は自由。`audience` は別キーになる） |
| 契約 | 1 | `contracts/topic_planning.py::AnalyticsSummary` |
| snapshot 直列化 | 2 | `report_to_payload` / `report_from_payload`（`AudienceShares` をそのまま保存。形は変えない） |
| 無関係 | 他 | `StrategyProfile.audience_*`、`TopicCandidate.audience_fit`（別概念） |

prompt に載る JSON のキーが `totals.audience` から `audience` へ移る。scoring（`domain/topic_planning/scoring.py`）は
`totals` を読まない（`audience_fit` は LLM の自己評価と Strategy の `preferred` から作る）。

## Alternatives

- **`totals["audience"]` を平坦化する**（`age_groups.age18-24` のような鍵）: 型注釈は満たすが、窓の合計と視聴者構成が
  同じ入れ物に混ざったままで、鍵の規則が暗黙になる。別フィールドの方が読み手にとって構造が明確
- **`totals` を `dict[str, Any]` にする**: 注釈を緩めて復号を通すだけで、形の食い違いが検出できなくなる（AGENTS.md §2 の
  「緩めて緑にする」と同じ）。採らない
- **送信側で `to_payload` が型を検査する**: Temporal SDK の挙動で、変えられない。往復を Activity 側で行う（Decision 2）
- **復号失敗で workflow を失敗させる（無限再試行を止める）**: workflow 定義の変更（patched）が要る。
  Activity 側で劣化させるほうが小さく、履歴の互換も壊さない

## Consequences

- 良い: 実 Analytics の内訳つきでも Topic Planner が最後まで通る。同種の食い違いは Activity 内で検出され、
  無限の workflow task 失敗ではなく `no_analytics` への劣化 + 警告になる
- 悪い: 劣化した場合、その日は analytics 抜きで企画される（`analytics_mode = no_analytics` として Plan に残る）
- 悪い: prompt に載る JSON の構造が変わる（`totals.audience` → `audience`）。prompt version は上げていない
  （prompt 本文が読む鍵ではなく、要約 JSON を丸ごと渡しているため）
- 未対応（他工程の調査結果）: Script / Storyboard / Production / Render / Upload の Activity 契約 dataclass に
  `dict` / `Any` / 入れ子の数値 dict の型は無く、この種の食い違いが起きにくい形（識別子・スカラー・ArtifactRef）。
  実 provider の出力は pydantic の Artifact 契約で Activity 内で検証され、失敗は Activity の失敗として分類される
