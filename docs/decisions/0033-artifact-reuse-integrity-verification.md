# ADR-0033: Artifact 再利用の完全性検証

## Status

Accepted (2026-09-23)

## Context

ADR-0032（統一再開）は「現行 Artifact が既にあれば再利用する」ことを INV-17 の帰結として
前提にしているが、その判定の実体である `ArtifactMetadataRepository.find_current`
（`infrastructure/db/repositories.py`）は次の列だけで決めている:

```python
stmt = select(ArtifactMetadataRow).where(
    ArtifactMetadataRow.episode_id == _as_uuid(episode_id),
    ArtifactMetadataRow.artifact_type == artifact_type.value,
    _artifact_scene_filter(scene_id),
    ArtifactMetadataRow.input_hash == input_hash,
    ArtifactMetadataRow.superseded_at.is_(None),
)
```

DB行が見つかれば「呼ばずに返す」（INV-17）。**MinIO に実体があるか、sizeとsha256が記録と
一致するか、JSONとして読み戻せるかを一切確認しない。** DB上は成功しているのに実体が
欠落・破損している状態（運用者による誤操作、MinIOの部分障害、旧版のバグ等）があれば、
統一再開はそれを「再利用可能」と申告し、Render が壊れた入力を掴んで見つけにくい形で
失敗するか、最悪 sha256 照合をしない工程では気づかれないまま完成動画に混入する。

Artifact の物理構造（`contracts/artifacts.py`）は既に2階層になっている:

1. `artifact_metadata` 行 + `bucket/object_key` にあるJSON記述子（`SceneImageArtifact` 等、
   `type`/`schema_version` を `Literal` で持ち `model_validate` で検証可能。`ArtifactMetadataRow.sha256`
   はこのJSON自体のsha256）
2. メディア系（`SCENE_IMAGE` / `SCENE_VOICE` / `SCENE_VIDEO` / `FINAL_VIDEO`）はJSON記述子の中に
   `MediaDescriptor`（`object_key` / `sha256` / `bytes` / `mime`）が入れ子になっており、
   **実バイナリは別オブジェクト**を指す。JSONだけ検証してもバイナリの欠落・破損は見えない

`ArtifactStore` Protocol（`infrastructure/storage/artifact_store.py`）には既に
`exists` / `stat` / `sha256_of`（流し読みでメモリに全体を載せない、`STREAM_CHUNK_BYTES=8MiB`）/
`download_to` があり、**これらの機能自体は既に実装済み**（本 ADR が新設するのは判定 ── 呼び方 ──
であって、検証プリミティブそのものではない）。

**実装時の訂正**: 当初案は `artifact_metadata` に `size` 列が無い前提で書かれていたが、
実装のため読み直した結果、`ArtifactMetadataRow.size_bytes`（nullable Integer）は**既に存在し、
既に全ての書き込み経路（`workers/production*/activities.py` / `workers/render/activities.py` /
`workers/upload/activities.py` / `workers/storyboard/activities.py` / `workers/planning/activities.py` /
`workers/dummy/activities.py`、いずれも `size_bytes=put.size` または `size_bytes=stored.size`）から
埋められている**。埋まっていないのは `domain.artifact.entities.ArtifactMetadata`（ドメイン実体）
と `infrastructure/db/repositories.py::_to_artifact`（DB行→ドメイン実体への変換）がこの列を
読み落としているだけだった。したがって **§Decision 7 の migration は丸ごと不要**（後述、修正版）。

## Decision

**再利用判定を「DB行の存在」から「DB行 + 実体の完全性検証」へ変える。既存の `find_current` は
残し（一覧・監査用途で今も使われる）、再利用の**唯一のゲート**として新しい関数を置く。**

### 1. 検証レイヤー（純粋関数 + I/O層を分離）

- `domain/artifact/verification.py`（新規、I/O無し、`contracts/` のみに依存）: 検証結果の型
  （`ArtifactVerdict`: `REUSABLE` / `MISSING` / `CORRUPT_HASH` / `CORRUPT_SCHEMA` /
  `VERSION_MISMATCH`）と、収集済みの事実（DB記録値 vs 実体から読んだ値）から verdict を1つに
  決める純粋関数。**推測しない**: 判定できる事実が無ければ `MISSING`/`CORRUPT_*` 側に倒す
  （安全側。ADR-0013 の「曖昧なら止める」思想と同じ）
- `infrastructure/artifact/verify.py`（新規）: 実際に `ArtifactStore.exists` / `stat` /
  `sha256_of` を呼び、JSON型は `contracts.artifacts.parse_*`（既存、`schema_version` は
  `Literal` 型で pydantic が既に検査する）で読み戻し、`MediaDescriptor` を持つ型は入れ子の
  バイナリも同じ手順で検証し、結果を `domain/artifact/verification.py` の純粋関数へ渡して
  verdict を得る。ここが本 ADR の実体

### 2. 検証項目（§Context の「実体から」全てを満たす）

1. **input_hash 一致**（既存 `find_current` のまま。変えない）
2. **schema_version 互換性**: JSON記述子を `contracts.artifacts.parse_*` で読み戻す。
   `Literal["1.0"]` 等の型検査に落ちれば `CORRUPT_SCHEMA`（pydantic が既に持つ検査を再利用する。
   自前でスキーマ再実装しない）
3. **生成設定版の互換性**: 型ごとに既存の単一宣言元と突き合わせる（新しい設定版の概念・列は
   増やさない）。

   **実装時の訂正**: 当初案は `SCENE_IMAGE` / `SCENE_VIDEO` の「現在有効な値」を
   `infrastructure/providers/fal_seedream_image.py` の `SEEDREAM_PROFILE_ID` /
   `fal_seedance_video.py` の `SEEDANCE_PROFILE_ID` という**固定定数**に決め打っていた。
   実装してテストを流した結果、これは実際には間違っていた: fake generator を使うテスト
   （`tests/support/production.py` の `FakeImageGenerator` / `FakeVideoGenerator`）は
   本物の fal 定数とは異なる `generation_profile_id`（例: `"fake-image-profile-v1"`）を
   正当に使っており、固定定数と比較すると**常に** `VERSION_MISMATCH` になってしまい、
   fake provider を使う既存の統合テスト（`tests/unit/test_production_image_activities.py` /
   `test_production_video_activities.py`）を壊した。根本的にも、「有効な値」を
   `infrastructure/artifact/verify.py` へ固定で埋め込むのは誤りで、本当に効いているのは
   provider・モデルではなく「今まさに構成されている generator が何を報告するか」である。

   是正・再訂正: 最初は `generator.generation_profile_id` を直接使う案にしたが、これも
   `test_production_video_activities.py` を壊した。原因は `workers/production_video/activities.py`
   が Artifact へ実際に書く `generation_profile_id` は `generator.generation_profile_id` そのもの
   ではなく、Activity 側で合成した値（`f"{self._generator.generation_profile_id}+{self._motion.motion_profile_id}"`、
   同ファイルの `_generation_profile_id` プロパティ）だったため。`PaidJobRunner` は video 固有の
   「motion profile」という概念を知らず、知るべきでもない（provider中立）。

   最終形: `PaidJobSpec` に `current_generation_profile_id: str | None = None` を足し、
   **各 Activity が自分の知っている「本当の現在値」を渡す**（image は
   `self._generator.generation_profile_id` そのまま、video は合成済みの
   `self._generation_profile_id`）。`PaidJobRunner.submit()` は `spec.current_generation_profile_id`
   を読んで `find_and_verify_current` → `verify_artifact` へ渡すだけで、値の作り方には関与しない。
   fal の固定定数を `infrastructure/artifact/verify.py` から完全に無くしたので、fal adapter を
   import する必要も無くなった（`tests/architecture/test_no_live_calls.py` の
   `FAL_ADAPTER_IMPORTERS` allowlist への追加は不要になった。当初案にあった追加は撤回した）。
   fake/real どちらの generator でも同じ形で正しく効き、provider・モデル・合成方法を差し替えても
   `infrastructure/artifact/verify.py` の変更が要らない。

   - `SCENE_IMAGE` / `SCENE_VIDEO`: `GeneratorMetadata.generation_profile_id` を、呼び出し元が
     渡す `current_generation_profile_id` と比較する。渡さない呼び出し元（この型を扱わない
     render/upload 等）ではこの検査を行わない（推測しない）
   - `SCENE_VOICE`: Piper（ローカル非課金・決定論的）の `generation_profile_id` は固定値ではなく
     `voice_id`/話速から都度導出される（`infrastructure/providers/piper_voice.py`）。**型そのものが
     この検査の対象外**（`current_generation_profile_id` を渡しても評価しない）。誤りではなく、
     この型にこの検査が意味を持たないだけ
   - `FINAL_VIDEO`: `render_profile.profile_id` を `contracts/render.py` の `RENDER_PROFILES`
     （既存の唯一の宣言元、`get_render_profile` が同じ参照をする）と照合する。render は
     provider を差し替えないので、この型だけは固定の宣言元との比較のままでよい
   - それ以外（`SCRIPT` / `STORYBOARD` / `PRODUCTION_MANIFEST` / `UPLOAD_RECEIPT` / `DUMMY`）は
     生成設定版という概念を持たないため、この検査を行わない
4. **MinIO 実体の存在**: JSON記述子オブジェクト自身、および（あれば）入れ子の
   `MediaDescriptor.object_key` の両方について `store.exists`。無ければ `MISSING`
5. **size と sha256 の実体照合**: `store.stat` の `size` を `MediaDescriptor.bytes`
   （メディア本体）または JSON記述子自身は `ArtifactMetadataRow.size_bytes`（**既存列**、
   §7 参照）と比較する軽い事前検査の後、`store.sha256_of`（流し読み、既存プリミティブ）で本検査。
   どちらか不一致なら `CORRUPT_HASH`
6. **streaming**: 5 は既存の `sha256_of`/`STREAM_CHUNK_BYTES` をそのまま使う。**新しい
   ストリーミング実装は作らない**（既に INV-9 の機械検査 `scripts/smoke.sh` が使っている経路と
   同じ）

### 3. 呼び出し箇所（通常パイプラインと resume の共通化）

`PaidJobRunner.submit()`（`infrastructure/production/paid_job.py`）の
`ArtifactMetadataRepository(session).find_current(...)` 呼び出しを、検証込みの
`find_and_verify_current(...)`（新関数、内部で `find_current` → 見つかれば §1 の verify を呼ぶ）
に差し替える。**実装時に判明**: Render・Upload にも同じ形の「既存なら再利用してよいか」
判定が既に**独立に実装されていた**（`workers/render/activities.py` の `_reusable`、
`workers/upload/activities.py` の `_reusable_receipt`）── どちらも JSON 読み戻し + sha256 照合を
自前で行っており、size 検証と生成設定版検証を持たない縮小版だった。これは AGENTS §8 が
禁じる「判定ロジックの複製」そのものだったため、両方とも `find_and_verify_current` へ差し替え、
render の `_reusable` は削除、upload の `_reusable_receipt` は「完全性は検証済みの
Artifact を受け取り、video_id が一致するかという業務判定だけをする」薄い関数
（`_matching_receipt`）へ縮小した。**新しい判定ロジックを工程ごとに複製しない**（AGENTS §8）。

`find_current_by_type` / `list_current_by_type`（render の manifest 組み立て・upload の
`_current_final_video` が上流 Artifact を**入力として読む**箇所）は対象外のままにした:
これらは「再生成をスキップしてよいか」の判定ではなく「この工程の入力は何か」を読むだけで、
`workers/upload/activities.py::_load_json` が既に descriptor 自身の sha256 を照合している
（本ADRのスコープはあくまで**再利用（skip）判定**であり、あらゆる Artifact 読み取りに
毎回ストリーミング検証を強制すると無関係な処理まで遅くなる、§Alternatives (a) と同じ理由）。

`domain/pipeline/resume_plan.py`（ADR-0032）の dry-run は Artifact 単位の判定を行わない
（§5 の訂正参照）。「再利用可能」という申告は dry-run には無く、実際の再利用判定は
各工程の Activity が実行される瞬間にだけ、この同じ関数を通じて行われる。

`VERSION_MISMATCH` / `MISSING` / `CORRUPT_*` の場合、`find_and_verify_current` は
`None` を返す（＝「現行が無い」として扱う）。呼び出し側は通常のフローに戻り、
**新しいラウンドとして再生成へ進む**（regenerate。retry と同じ既存経路、新しい状態を増やさない）。
これが「retryとregenerateを区別する」の実装そのもの: input_hash 一致 かつ 検証通過 なら retry
（呼ばずに返す）、それ以外（input_hash不一致、または検証不通過）なら regenerate
（新しいラウンドで呼ぶ）。

### 3.1 実装後に発見した第2のゲート: `await_output` の `outcome_artifact_id` 早道（実装時の訂正）

一続きの故障再現シナリオ（`tests/integration/test_incident_recovery_e2e.py`）を組んだところ、
§3 で配線した `find_and_verify_current`（submit 時の「既存を再利用してよいか」判定）だけでは
不十分なことが分かった。`PaidJobRunner.await_output`（および内部で呼ぶ
`_output_after_spent`）には、予約に `outcome_artifact_id` が**既に**紐づいている場合の
早道があり、そこは `ArtifactMetadataRepository.get(...)` で行を取得するだけで
`find_and_verify_current` を経由していなかった。

具体的な事故筋書き: シーンの動画が一度成功して `outcome_artifact_id` が紐づいた後、
その実体（MinIO）だけが外部から破損する → 同じラウンドの `await_output` が再実行される
（Activity 再試行・workflow 再開のいずれでも起こりうる）→ 予約はもう「完了済み」なので
`find_and_verify_current` の対象にすらならず、破損した Artifact をそのまま「検証済みの
成功」として返していた。これは本ADRが閉じようとしていた問題そのものが、別の入口から
すり抜けていたことを意味する。

**修正**: `await_output` と `_output_after_spent` の両方に `_verified_outcome_artifact(...)`
（内部で `verify_artifact` を呼ぶ）を追加した。`outcome_artifact_id` が指す Artifact は、
返す**前**に必ず実体まで検証する。検証に落ちたら「紐づいていない」のと同じに扱い、
下段の evidence（`raw_output_key` の生の取得物）から検証をやり直す経路へ自然に落ちる
（新しい分岐を増やさない。既存の「spent と Artifact 記録の間で落ちた」経路の再利用）。

**この経路での破損検出後の実際の着地点**: evidence（生の取得物）は無傷でも、書き込み先の
content-addressed キーには既に（破損した）別内容が入っているため、immutability（INV-11）が
黙った上書きを禁じ、`ArtifactConflictError`（needs_input）で安全に止まる。「壊れたら黙って
直す」よりもこちらの方が安全である: 破損の自動修復は「検出しても自動で削除・変更しない」
という §Decision(4) の方針と本質的に衝突するため、正しい着地点は
「needs_input で人に見せる」である（自動修復ではない）。これは
`tests/integration/test_incident_recovery_e2e.py::test_corrupt_artifact_is_never_silently_reused_or_silently_overwritten`
で実際に検査している。

`infrastructure/production/paid_job.py` は元々 ADR-0030 の一部として既にレビュー済み・
統合済みだったファイルである。今回の変更はその既存コードへの追加のバグ修正であり、
ADR-0030 の決定を覆すものではない。`tests/unit/test_paid_job.py` に
`test_await_output_does_not_return_a_corrupted_linked_artifact_silently`（この経路が
壊れた Artifact を黙って返さないことの直接証明）と
`test_await_output_still_returns_a_valid_linked_artifact_fast`（非退行: 実体が無傷なら
従来どおり高速path のまま）を追加した。

### 4. 破損の扱い（削除しない・無条件再送しない）

- 検証で `MISSING`/`CORRUPT_*` になっても、既存の MinIO object・`artifact_metadata` 行・
  `provider_reservations` 行を**自動で削除・変更しない**。人間が調べられる証拠として残す
  （INV-15/INV-11 と同じ「自動で消さない」思想）。検証結果は構造化ログ
  （`ARTIFACT_VERIFICATION_FAILED artifact_id=... artifact_type=... verdict=... episode_id=...`）
  として残す。secretは含まない（該当箇所に元々secretは無い）
- **成否不明の Provider 予約（`reserved` + evidence無し）がある状態で、Artifact 破損だけを
  理由に無条件で新しい予約を作らない。** `find_and_verify_current` が `None` を返しても、
  §3 の呼び出し元（`PaidJobRunner.submit`）は既存の
  `find_unreconciled` チェック（ADR-0013、変更なし）を先に通る。破損検出は
  「現行Artifactが無いのと同じ」に倒すだけで、未照合予約のブロックを迂回するショートカットを
  作らない

### 5. TOCTOU: 検証は「決める瞬間」にだけ起きる（実装時の訂正）

**当初案の訂正**: 当初は「dry-run と実行がそれぞれ独立に `find_and_verify_current` を呼び直す」
と書いたが、実装のため `domain/pipeline/resume_plan.py` / `apps/api/routers/episodes.py` を
読み直した結果、`build_resume_plan` / `_load_resume_plan` は**工程レベルの入場可否**
（`target_stage` / `stages_to_run` / `possible_new_charges`）しか判定しておらず、
個々の Artifact を検証してはいなかった（これは ADR-0032 §Decision/§Consequences が
「Artifact 単位の精密な diff は本ADRのスコープ外」と自ら明記していた、既存の意図的な
スコープ限定と整合する）。

したがって実際の設計は次の通りになる: **`find_and_verify_current`（および §3.1 の
`_verified_outcome_artifact`）は dry-run からも POST からも呼ばれない。** 呼ばれるのは、
実際にどこかの工程（production / render / upload）の Activity が「この Artifact を
再利用してよいか」を**決める、まさにその瞬間**だけ（§3 の submit 時の3箇所 + §3.1 の
await 時の2箇所）。dry-run の `GET .../resume/plan` も実行の `POST .../resume` も、
この判定より**前**の工程レベルの計画段階にとどまる。

これは当初案より弱い保証ではない: 「事前に検証してキャッシュし、後で信用する」窓が
そもそも存在しないため、TOCTOU の隙間が構造的に無い。判定は常にその場で1回だけ行われる。
代償は、dry-run の `possible_new_charges` が「この工程は課金されうる」という工程単位の開示
のままで、「この特定のシーンは破損しているので確実に再生成される」という Artifact 単位の
事前警告は出せないこと（§Decision 1 / Consequences の開示スコープと同じ限界）。

### 6. キャッシュはしない

性能上、8GiBの動画を毎回流し読みするコストはある。しかし今回のスコープでは
**検証結果をキャッシュしない**（§Alternatives (d) 参照）。理由:再開の頻度は
1Episodeあたり数個のArtifactを高々数回読むだけであり、「正しさを性能のために省略しない」
という明示の指示に対して、キャッシュの無効化条件（object version/ETag/size/検証時刻）を
正しく設計するコストと危険（無効化漏れ）が、今回計測もしていない性能上の利益に見合わない。
将来ボトルネックになった場合の設計は本ADRの対象外として残す。

### 7. migration（実装時に不要と判明）

**migrationは無い。** `artifact_metadata.size_bytes` は既存列であり、既に全ての書き込み経路が
埋めている（§Context の訂正参照）。必要なのは `domain/artifact/entities.py::ArtifactMetadata`
に `size_bytes: int | None` を足し、`infrastructure/db/repositories.py::_to_artifact` で
DB行から読み取るだけ。**NULL のとき**（本ADR以前に書かれた行がもし存在すれば、その行だけ）は
「size未記録」として size 比較をスキップし sha256 検証だけで判定する（過去データの後方互換。
強制的な backfill はしない）。

## Alternatives

**(a) `find_current` 自体を検証込みに変える** — 呼び出し側が1箇所で済む。しかし
`find_current` は一覧・監査など「検証不要な軽い読み取り」にも使われており、そこにまで
毎回 MinIO への往復（大きい動画なら流し読み）を強制すると無関係な処理まで遅くなる。
検証が要る箇所だけが新関数を呼ぶ形にする。却下。

**(b) Shorts と長尺で別の検証ルールを持つ** — 尺によって検証の緩急を変える誘惑があるが、
「欠落・破損」はサイズに関係なく同じ意味である。検証ロジックは尺非依存にし、
`MEDIA_MAX_BYTES` 等の既存の尺依存パラメータだけが契約側で変わる。却下（ユーザーの要求どおり
同じ契約を使う）。

**(c) 破損を検出したら自動で `superseded_at` を立てて次の生成に道を譲る** — 一見親切だが、
「自動で消さない」という INV-15 と同じ思想に反する。人間が原因を見る前に証拠が消える。却下。

**(d) 検証結果を DB にキャッシュする（object version/ETag/size/検証時刻キー）** — 性能は
改善するが、無効化ロジック自体にバグが入れば「キャッシュが古いまま健全と報告する」という
本ADRが解こうとしている問題そのものを再発させかねない。計測前の最適化は避ける。却下
（§Decision 6 に理由を残す。将来ボトルネックが実測されたら別ADRで再検討）。

## Consequences

**良い側**
- DB行だけを信じた「見えない再利用ミス」が構造的に防げる。破損は resume・通常生成いずれの
  経路でも同じ判定で捕まる
- 破損検出時に証拠（object・metadata・reservation）が残るので、人間が原因を追える
- retry と regenerate の区別が「input_hash一致 かつ 検証通過」という1つの述語に集約される

**悪い側 / 引き受けた負債**
- 再利用のたびに MinIO への追加往復（`exists`/`stat`/`sha256_of`）が増える。大きい動画では
  流し読みのコストがある（§Decision 6 でキャッシュを意図的に見送った代償）
- `generation_profile_id` の「現在有効な値」は呼び出し元（`PaidJobRunner.submit`）が
  今まさに構成されている generator から渡す。渡し忘れた呼び出し元は、この検査が
  黙って未評価（`profile_check_applicable=False`）になる ── 検査漏れが静かに起きうる形であり、
  新しい呼び出し元を足すときは意識して渡す必要がある（§実装時の訂正、§2-3 参照）
- `size_bytes` が `NULL`（未記録）な行はsize比較をスキップするため、sha256のみの検証になる
  （sha256自体は内容の完全性を保証するので安全性は落ちない。size比較は「速い一次スクリーニング」
  でしかないため、これが無くても正しさは保たれる。実装時点でこの列は全書き込み経路が既に
  埋めているため、NULL行は理論上の後方互換ケースであり実データには通常存在しない）
- 破損検出後の**自動修復経路は無い**（意図的。§Decision 4）。運用者が見つけて人手で
  判断する前提であり、`failure-policy.md` §6 の「stalled はまず通報する」と同じ運用負荷を継承する
- `workers/production_image/activities.py` / `production_video/activities.py` には、
  `PaidJobRunner.submit()` を呼ぶ**前**に別の目的（Job の `start`/`skip` を決めるだけ）で
  `ArtifactMetadataRepository.find_current`（検証なし）を直接呼ぶ箇所がそれぞれ1つ残っている。
  ここが破損した Artifact を「ある」と誤認しても、実際の再利用可否は必ず
  `PaidJobRunner.submit()` 内の `find_and_verify_current` が最終的に決めるため、破損した
  Artifact が誤って再利用されることは無い。影響は Job の状態遷移が一時的にずれる程度の
  運用上の軽微な不整合に留まる。本ADRのスコープ（再利用の可否）ではないため、あえて手を付けず
  次の負債として明記する

## 機械検査

- `tests/unit/test_artifact_verification.py`（新規: `domain/artifact/verification.py` の
  純粋関数、全 verdict の分岐）
- `tests/unit/test_artifact_verify_io.py`（新規: `infrastructure/artifact/verify.py`。
  欠落・sha256不一致・size不一致・schema不正・profile不一致それぞれで正しい verdict、
  streaming で全体をメモリに載せないこと）
- `tests/unit/test_paid_job.py`（拡張: 破損Artifactは再利用されず新ラウンドへ進むこと、
  未照合予約があれば破損検出より先にブロックされること、削除・変更が一切発生しないこと）
- `tests/unit/test_artifact_store.py` / `tests/unit/test_repositories.py`（拡張: `size_bytes`
  がドメイン実体 `ArtifactMetadata` まで読み取れること。migrationは無いので
  `test_migration_matches_models.py` に変更は無い）
- `tests/unit/test_paid_job.py::test_await_output_does_not_return_a_corrupted_linked_artifact_silently`
  / `::test_await_output_still_returns_a_valid_linked_artifact_fast`（§3.1、新規: 第2のゲート）
- `tests/unit/test_render_activities.py::test_final_video_with_a_retired_render_profile_is_version_mismatch`
  （独立レビュー指摘: `FINAL_VIDEO`/`RENDER_PROFILES` の版失効分岐、当時無検査だった）
- `tests/integration/test_incident_recovery_e2e.py`（一続きの故障再現シナリオ。§3.1 の発見元。
  `test_sb6_403_blocks_then_recovery_resumes_only_sb6_without_recharging` /
  `test_corrupt_artifact_is_never_silently_reused_or_silently_overwritten` /
  `test_auth_incident_threshold_suppresses_new_submits_for_same_provider_only` /
  `test_watchdog_flags_stopped_pipeline_before_resume_then_resume_recovers`）
