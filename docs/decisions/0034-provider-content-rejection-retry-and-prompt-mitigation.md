# ADR-0034: provider のコンテンツ拒否は同じ入力で自動再送しない・拒否理由を全文残す・写実的な肖像を避ける

## Status

Accepted (2026-09-28)

## Context

2026-09-26・2026-09-27 の2日連続で、Episode `dedcf315-256f-4769-9baf-6d4bb6bf6dfa`（sb4）と
`ec44fadd-3de8-44af-9060-ad538c93d103`（sb2）が `blocked` になった。両方とも画像生成
（12件）は全部成功し、特定シーンの**動画生成だけ**が fal から拒否された:

```
HTTP 422 types=['content_policy_violation']
loc: ['body','image_url']
msg: 'The images or videos provided may contain likenesses of real people or
      other private information that cannot be processed.'
ctx.extra_info.reason: 'partner_validation_failed'
```

**証拠**: `infrastructure/providers/fal_seedance_video.py` の `build_seedance_payload` が
`image_url` として渡すのは、直前の `produce_scene_image`（Seedream）が生成した画像の URL
（fal CDN へアップロード済みのもの、`FalSeedanceVideoGenerator.prepare`）である。拒否の
`loc` が `body.image_url` を指しているので、**拒否の対象は動画モデル（Seedance）に渡した
入力画像そのもの**であり、動画側のテキストプロンプトではない。

`ec44fadd` sb2 のプロンプト冒頭は "Ieyasu stands on a low ridge in armor..." で、実在の
歴史上人物（徳川家康）を明示的に名指ししている。fal 公式の `content_policy_violation`
説明（実在人物の肖像・個人情報）と一致度が高い（**証拠に基づく強い仮説**）。

`dedcf315` sb4 のプロンプト冒頭は "Grounded scene inside a fortified temple compound:
Buddhist monks wear robes layered over lamellar armor..." で、名指しの実在人物名は無い。
生成された**画像そのもの**が結果的に特定の実在人物に似ていた可能性（fal 側の顔検出が
生成結果へ反応した）はテキストだけでは判定できず、**未確定**のままにする（実際の生成画像を
MinIO から取得して人手で見比べない限り確証は得られない）。

調査で追加に判明した2つの問題:

1. **同じ入力の自動再送**: `infrastructure/production/paid_job.py::_plan_round`
   （ADR-0017 §3 の元の表）は、`spent` 済みで取得物が無い予約（受理されず／provider の拒否を
   含む）を無条件に「最新ラウンド + 1」として次の submit へ進めていた。`ProviderRejectedError`
   （needs_input、docs/failure-policy.md）は「同じ入力を再送しても同じ結果になる」ことが
   分類の前提なのに、Episode を resume すると `ProductionWorkflow` は round=1 から数え直し、
   `_plan_round` が同じ `input_hash` のまま新しい予約を作って `generator.submit()` を
   もう一度呼んでいた（同じシーンの画像は再利用されるが、拒否された動画側だけ再送される）。
   単一の workflow 実行の中では `workers/production/workflows.py::_rounds` が
   `NEW_ROUND_ERROR_TYPE_NAMES`（`ProviderJobFailedError` / `MediaValidationError` だけ）で
   この種の自動進行を止めているが、**resume を跨ぐと台帳（`_plan_round`）にはその区別が無い**。
2. **拒否理由の切り詰め**: `jobs.error_summary` は DB 列としては 2000 文字まで許容するのに、
   実際に記録された値は 584 文字で単語の途中に切れていた。原因は
   `infrastructure/providers/fal_queue.py::_short`。fal の 422 は pydantic 形式の
   `{"detail": [{"type": ..., "msg": ..., "loc": [...], "ctx": {...}}]}` を返すが、
   `_short` は `str(body["detail"])`（Python の repr 表現）を素朴に 500 文字で切っていたため、
   `msg` の文中で切れ、運用者が拒否理由を読めなかった。

## Decision

### 1. 分類は変えない（確認）

`content_policy_violation`（HTTP 422）は既にこの起点のコードで `ProviderRejectedError`
（needs_input）に分類されている（`infrastructure/providers/fal_queue.py::result`）。この
起点には ADR-0030〜0033 が既に入っており、401/403 の `ProviderUnavailableError` とは別の
型・別の意味論（人間がプロンプト・素材を直す）で扱われている。**この分類自体は妥当**なので
変更しない。

### 2. 同じ input_hash では自動で新しいラウンドへ進まない

`provider_reservations` に `input_rejected_by_provider`（bool、既定 false）を追加する
（migration `0013_provider_reservation_rejected_input.py`）。provider が入力そのものを
拒否したとき（`isinstance(exc, ProviderRejectedError)`）だけ true にする
（`infrastructure/production/paid_job.py::_spend_conservatively`。型で決める。文字列一致
では決めない）。

`_plan_round` はこの列が true の `spent` 予約を見つけたら、**新しいラウンドを作らず**
`ProviderRejectedRetryBlockedError`（`ProviderRejectedError` のサブクラス、`domain/errors.py`。
needs_input を継承）を送出する。予約 INSERT も `generator.submit()` の呼び出しも起きない
（課金無し）。

回復は次のどちらかだけ:

- 人間がプロンプト・素材を直し、**新しい `input_hash`** を作る（同じシーンでも新しい
  `input_hash` に対する `find_latest_for_input` は空なので、通常どおりラウンド1から進む）
- （本設計では用意しない）同じ `input_hash` のまま再送する経路 ── 台帳は append-only で
  `spent` から自動で戻らない設計（ADR-0013）と整合させるため、あえて作らない

`ProviderUnavailableError`（401/403）はこの新しいゲートの対象に**含めない**。認可障害は
既に ADR-0030 の `ProviderCredentialSuspectedOutageError`（時間窓ベースの抑止、成功で自動解消）
が別の粒度で扱っており、同じ入力を「credentials を直した後」に再送するのは正しい回復経路
だからである。`input_hash` 単位で永久に塞いでしまうと、この正しい回復経路を壊す。

### 3. 拒否理由を切り詰めない

`infrastructure/providers/fal_queue.py::_short` を、構造化された `detail`（pydantic
validation error のリスト）を `type: msg (at loc) [reason]` の形に整形してから切るよう
変更する。上限を 500 → 800 文字に上げる（workflow 側の `_summary()` 1000 文字、DB の
2000 文字にまだ余裕を残す）。整形できない形（リストでない・辞書でない）は従来どおり
素朴な文字列化+切り詰めにフォールバックする。

### 4. 実在人物の写実的な肖像を避けるプロンプト・スタイル変更

`domain/production/prompting.py` の画像・動画プロンプト組み立てに、「実在・歴史上人物の
写実的な顔の再現ではなく様式化した挿絵として描く」制約を追加する
（`_CONSTRAINTS` / `DEFAULT_IMAGE_STYLE.style`）。**史実の描写自体は変えない**: 台本・
storyboard が "Ieyasu" のような実在人物名を明示することは止めない（描いていないと偽る
婉曲表現はしない）。変えるのは**画風**だけで、鎧・旗指物・場面設定などの記号的表現で
人物を示しつつ、fal が挙げた拒否理由（「実在人物の肖像に見える可能性」）そのものへ対処する。

これは fal の safety checker の内部実装を確認できないため、**確証ではなく根拠のある
緩和策**である（fal 自身が挙げた拒否理由と直接対応するという意味で「根拠のある」）。
効果が無ければ、この起点に既にある `ProviderRejectedError` → `needs_input` のまま安全側に
倒れ、人間の判断を待つ（今回の変更で自動再送を止めたので、効果が無くても無駄な再送・
再課金は起きない）。`dedcf315` sb4（名指しの人物名が無いケース）については、実際に
生成された画像を見比べない限りこの変更が効くかどうか確定できないことを明記しておく
（**未確定**のまま）。

`IMAGE_PROMPT_BUILDER_VERSION` を `"1"` → `"2"`、`VIDEO_PROMPT_BUILDER_VERSION` を
`"1"` → `"2"` に上げる（`style_profile_id` / `motion_profile_id` が `input_hash` に入るため、
文面を変えたら版を上げる規約どおり。上げないと古い画像が新しい規則の結果として誤って
再利用される）。

## Alternatives

- **全ての needs_input 失敗で `_plan_round` の自動進行を止める**: 採らない。
  `ProviderUnavailableError`（401/403）を含めてしまうと、ADR-0030 の「credentials を直せば
  次の成功で自動解消する」という正しい回復経路を、`input_hash` 単位の永久ブロックで壊す。
  `ProviderRejectedError` だけを対象にする（型で判定する）ことで、この副作用を避けた。
- **`error_summary` の文字列をそのまま拒否理由の判定に使う（例: "content_policy_violation"
  を含むか grep）**: 採らない。AGENTS.md §「散文のgrepで分類しない」。判定は必ず例外の型で行う
  （`isinstance(exc, ProviderRejectedError)`）。文字列は人間向けの説明にとどめる。
- **プロンプトを婉曲表現で書き換え、実在人物を描いていないと偽る**: 採らない。タスクの
  明示的な禁止事項であり、史実の正確さを損なう。写実性（画風）だけを変える。
- **fal のリクエストへ seed 固定・別モデルへの自動切替などで「別の入力」を機械的に作り、
  content policy を回避できるまで自動で試行し続ける**: 採らない。fal のこれらのエンドポイント
  には seed パラメータが無く（`infrastructure/providers/fal_seedance_video.py` のコメント）、
  「別モデルへの自動切替」は生成品質・コストの前提を変える大きな決定で、今回の事故の
  スコープを超える。安全側（needs_input のまま止める）を優先した。
- **`provider_reservations.reconciled_by` の値を使い分けて機械判定する
  （例: `"conservative:rejected"`）**: 採らない。`reconciled_by` は「誰がどう照合したか」の
  文字列で、docs/decisions/0017 が固定の語彙（`evidence` / `conservative` /
  `operator:<id>`）として文書化しており、新しい部分文字列を混ぜると既存の文字列一致に
  依存する読み手を壊しうる。専用の bool 列（`input_rejected_by_provider`）を追加する方が
  機械判定として明確（AGENTS.md §8: 判定用の値は1箇所に）。

## Consequences

良い:

- 同じ入力のまま content policy 拒否を繰り返して課金・時間を浪費する経路が塞がる
  （`tests/unit/test_paid_job.py::test_provider_rejected_input_blocks_the_next_round`）
- プロンプトを直せば通常どおり新しいラウンドとして進める
  （`tests/unit/test_paid_job.py::test_provider_rejected_input_does_not_block_a_different_input_hash`）
- 拒否理由が省略されずに残るので、次に同じ理由で止まったときに人間がすぐ判断できる
  （`tests/unit/test_fal_queue.py::test_content_policy_rejection_keeps_the_full_reason_without_cutting_mid_word`）
- 実在人物を扱うシーンで content policy 拒否が減る可能性がある（確証は無い）

悪い（引き受けた負債）:

- `dedcf315` sb4 のように名指しの人物名が無いケースへの効果は未確認のまま出荷する。
  次に同種の拒否が起きたら、MinIO 上の実際の生成画像を人手で確認する運用が要る
  （自動化しない。仮説の検証コストを先送りした負債）
- `input_rejected_by_provider` が true になった予約は、**同じ input_hash では永久に**
  次のラウンドへ進めない（台帳が append-only なので、abandon 相当の「同じ入力のまま
  再送を許可する」経路を意図的に作らなかった）。誤検知（実在人物ではないのに fal が
  誤って拒否した場合）でも、回復は「プロンプトを変えて新しい input_hash を作る」しかない。
  運用上、内容を変えたくないのに再送したいケースへの逃げ道が無い
- スタイル変更は `IMAGE_PROMPT_BUILDER_VERSION` / `VIDEO_PROMPT_BUILDER_VERSION` を上げた
  ため、次にどのシーンでも画像・動画が作られるときは新しい画風で**再生成（課金）**される。
  影響はプロンプトが触れる全シーンに及ぶ（意図的だが範囲は広い）
