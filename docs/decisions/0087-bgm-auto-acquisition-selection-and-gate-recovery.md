# ADR-0087: BGM の自動収集・自動選曲・フォールバックと、品質ゲートの検査障害からの回復

## Status

Proposed（2026-11-02。所有者の依頼「BGM自動収集・選曲・合成とQualityGate改善」に基づく。
ADR-0083 §1・§2 の一部と §6（INV-133 / INV-136）を**所有者の明示判断として**改める。本番適用と flag の ON は所有者の別承認）

## Context

1. ADR-0083 §1 は「第三者の音源は取得しない（エージェントはライセンスを確認できない）」とし、承認は人だけが行う（INV-133）。
   その結果、カタログは所有者が手で置いた曲だけで、曲が尽きると BGM 無しになる。所有者は「毎回介入せず音楽付きの動画を
   継続的に自動制作・投稿」を求め、**機械的に検証できるライセンスに限り**自動承認を許した（本 ADR の依頼文 §3.2 / §11）。
2. 実在の候補 5 曲（`~/.local/share/avp2/bgm-candidates-20261102/`）はいずれも Wikimedia Commons / incompetech 由来で、
   Commons の 3 曲は MediaWiki API の `extmetadata` だけで「CC0・本人の作品」を機械的に確かめられた（`commons_api.json`）。
   incompetech（CC BY）と「鹿の遠音」（PD 録音。録音の権利が国ごとに違う）は機械的に確定できない。
3. 所有者が試聴・承認したのは **CK61 の +6 dB 版**。実測では全 cue が `BGM_NARRATION_PRIORITY_MAX_DB`（-12）に張り付き、
   発話中の声との差は最小 14.2 LU（閾値 12 LU）。+0 dB 版は声より約 20 LU 低く、スマートフォンでは聞こえない。
   鹿の遠音は +3 dB で 12 LU を割る。曲ごとに安全な持ち上げ幅が違う。
4. カタログは作業ディレクトリの `catalog.json`（ADR-0077 §1）で、cue sheet を置く運用者がいない（INV-134 の未配線）。
   Episode ごとの選曲・使用履歴・取得の監査が残らない。
5. 本番 Episode 58 件（2026-11-02、読み取り専用で集計）: blocked 17 件の内訳は fal の HTTP 403（認証）13・fal の月次上限 2・
   LLM 出力の検査（ナレーション予算超過・storyboard schema 違反）2。**品質ゲート起因は 0 件**（`AVP_QUALITY_GATE_ENFORCE` は
   一度も ON になっていない。`quality_check_records` は 0 行）。「blocked = 品質ゲートが厳しい」は本番データでは成り立たない。
6. それでも ON にした後の過剰停止は構造上起きる: `QualityGateService` は producer の**一時的な失敗**（MinIO の瞬断・DB の
   タイムアウト）を「記録が無い」として扱い、必須の検査が欠けるので REVIEW → Episode は BLOCKED で止まる。品質の問題と
   検査系の故障が同じ REVIEW になり、再試行も無い。

## Decision

**機械的に検証できる自由ライセンスの曲だけを、上限つきの別ワークフローで自動収集して DB カタログに隔離登録し、
決定的な規則で自動承認・自動選曲する。選曲と配置は Render の内側で Episode ごとに 1 回だけ決めて永続化し、
失敗は上限つきの代替曲・減衰・BGM 無しへ落とす。品質ゲートは検査系の一時障害を内部で再試行し、品質の判定と区別して記録する。**

### 1. 音源ソースの扱い（候補と自動取得を分ける）

`contracts/music_sources.py` が**唯一の定義**。各ソースは `search_allowed` と `auto_acquire_allowed` を別々に持つ。

| ソース | 検索候補 | 自動取得 | 理由 |
|---|---|---|---|
| Wikimedia Commons（MediaWiki API） | ○ | ○（CC0 のみ自動承認） | 公開 API・User-Agent 方針と速度制限が明文化。ファイルごとのライセンスを `extmetadata` で機械的に取得でき、原本 URL が安定 |
| Free Music Archive | ○（手動） | × | 公開 API は停止。ダウンロードはサイト経由・規約上の自動取得の許可が無い |
| Pixabay Music | ○（手動） | × | Pixabay API は画像・動画のみで音楽を含まない。自動ダウンロード・スクレイピングは規約で禁止。Content ID の申し立て例が多い |
| ccMixter | ○（手動） | × | 多くが CC BY / BY-NC で、帰属文の正確な自動化が未検証。初期は対象外 |
| incompetech | ○（手動） | × | CC BY。API 無し。帰属文は所有者が確認して手で登録する |

規約の調査結果（取得日・URL・要点・証拠の保存方法・停止時の代替）は `docs/research/bgm-sources-2026-11.md` に置く。
規約が変わったら表と文書を同じコミットで更新する。**スクレイピングは実装しない**（API と原本 URL の取得だけ）。

### 2. 自動承認の規則（INV-150 / INV-151）

自動承認は純関数 `domain.music.license_policy.decide_license`。入力は API の応答（保存した証拠そのもの）から作った
`LicenseFacts` で、出力は `AUTO_APPROVE` / `REVIEW` / `REJECT` と理由コード。

- `AUTO_APPROVE`: ソースが `auto_acquire_allowed`、ライセンスが **CC0 1.0**（`LicenseShortName` と `License` の正規化が一致）、
  作者の申告が「本人の作品（Own work）」、`Restrictions` が空、ファイルが削除・差し替え候補でない、メタデータに矛盾が無い
- `REVIEW`（隔離 `quarantined` + `unverified`。選曲に出ない）: CC BY / CC BY-SA（帰属・継承の条件の自動確定は未実装）、
  パブリックドメイン表示（録音の権利が国ごとに違う）、作者が本人でない CC0、メタデータが部分的に欠ける
- `REJECT`（登録しない）: NC / ND / 権利不明 / 出典不明 / ライセンス表示が無い・矛盾する

承認の主体は `license_approver_kind`（`owner` / `policy`）として記録する。`policy` の承認は**規則の版**
（`BGM_LICENSE_POLICY_VERSION`）と**証拠の sha256**（保存した API 応答の正準 JSON）を必ず持つ。規則の版を上げたら
`policy` 承認の曲は次の選曲までに再判定される（古い規則の自動承認を引き継がない）。所有者の承認は規則の版に依らない。

ライセンスの有効性と **Content ID の申し立てリスクは別の項目**（`content_id_risk`: `unknown` / `low` / `elevated`）。
自動取得した曲は `unknown` で、選曲の点数を下げるが除外はしない（投稿は private だけ。CLAUDE.md §9）。

### 3. 収集処理（INV-152）

`BgmAcquisitionWorkflow`（専用のタスクキュー `bgm-acquisition`、Episode ワークフローとは独立）。
検索条件の生成 → 許可ソースの検索 → メタデータ取得 → `decide_license` → 許可された原本 URL からのダウンロード →
形式（ffprobe で音声ストリーム・コンテナ）・サイズ・尺・整合性の検査 → sha256 で重複検出 → MinIO
`music/originals/<sha256>.<ext>` へ保存（読み戻して照合）→ 証拠（API 応答の正準 JSON・取得 URL・時刻）を MinIO
`music/evidence/<sha256>.json` へ保存 → DB カタログへ登録 → ラウドネス・テンポの解析 → 状態を決める。

- **ダウンロード直後の曲を `active` / `approved` にしない。** 登録は常に `quarantined`。解析が終わり、`decide_license` が
  `AUTO_APPROVE` のときだけ同じトランザクションで `active` + `approved`（`policy`）へ進む
- 上限（`contracts/music_sources.py` の定数）: 1 回の実行で検索 4 回・ダウンロード 3 件・1 件 40 MB・合計 120 MB・
  1 件の尺 30 秒〜15 分・実行全体 10 分。日あたり 1 回（同じ需要の hash の workflow id を再利用しない）
- ネットワークは `infrastructure/music/sources/` だけが触る（アーキテクチャ検査）。render / production / upload は import しない
- 有料の音楽サービスは導入しない。fal・YouTube の予約台帳には触れない

### 4. カタログの永続化（migration 0021、expand-only）

ADR-0077 末尾の計画を実施する。`0020` は他の枝（`claude/agent-tools-api` の `agent_tool_calls`、ADR-0081 追補の TRUNCATE 拒否）が
予約しているため **0021**（`down_revision = "0019"`）。後から 0020 が入ったら、後に入る側が `down_revision` を付け替える。

- `bgm_tracks`: `BgmTrack` の全列 + `license_approver_kind` / `license_policy_version` / `content_id_risk` /
  `acquired_from` / `acquired_at` / `evidence_object_key` / `allowed_brands` / `analysis`（実測: ラウドネス・true peak・尺・
  テンポ推定。JSONB）/ `inferred_tags`（推定: ジャンル・感情・雰囲気。JSONB。**ライセンスの列と分ける**）/
  `mix_gain_offset_db`（所有者が試聴で決めた持ち上げ幅。既定 0）/ `last_verified_at`。`file_sha256` は UNIQUE
- `bgm_selections`: Episode ごとの選曲の決定（`episode_id` UNIQUE。要件・候補の点数と理由・選んだ曲・適用 gain・cue sheet の
  正準 JSON と sha256・フォールバックの経過・規則の版）。**1 Episode 1 行**で、再実行は既存の行を返す
- `bgm_acquisition_runs`: 収集の実行ごとの監査（需要の hash・検索語・候補・判定・ダウンロード結果・バイト数・sha256・時刻）
- 使用履歴・利用回数は `bgm_selections` の集計（列を二重に持たない）

3 表とも他表への FK を張らない（ADR-0077 の計画どおり単独で DROP して戻せる）。`episode_id` は UUID の値として持つ。
既存の `LocalDirectoryBgmSource` は残し（プレビュー用ツール `scripts/bgm_preview.py` が使う）、render worker は
`BGM_ENABLED` が ON のとき DB 版 `DbBgmSource` を使う。DB 版は MinIO の原本を作業ディレクトリへ取り出して sha256 を照合する。
所有者が手で置いた曲（CK61 など）は `scripts/bgm_catalog_import.py` で DB へ移す（承認は持ち越さず、取込後に所有者が承認する）。

### 5. 自動選曲（INV-153）

`domain.music.requirements.derive_requirements(script, storyboard, brand_policy)`（純関数・LLM を使わない）が
`MusicRequirements`（mood・energy・tension・tempo の帯・文化圏・時代・ナレーション密度・場面の切替頻度・尺・形式）を作る。
語彙表は `domain/music/lexicon.py` が唯一の定義。LLM は補助として**使わない**（初期実装。必要になったら別 ADR）。

`domain.music.scoring.score_candidates(requirements, catalog, usage, now)` は `select_approved`（法的な門。INV-97）を通った曲
だけを、説明可能な項目ごとの加点で並べる（mood 一致・energy・tempo 帯・文化圏・尺・直近の使用の少なさ・Content ID リスク・
ブランドの許可）。各項目の点と合計を `ScoreBreakdown` として返し、同点は `track_id` で決める。**同じ入力は同じ順序**。
最低点 `MIN_SELECTION_SCORE` に届く曲が無ければ「適切な曲が無い」とし、需要（`MusicRequirements` の正準 hash）を
`bgm_acquisition_runs` に積む。**その Episode は待たない**（BGM 無しか、点の足りない中の最上位の承認済み曲）。

Episode ごとの決定は **Render の内側**（`DbBgmSource.load(episode_id, plan, script, storyboard)`）で 1 回だけ作り、
`bgm_selections` に保存する。2 回目以降（再試行・再実行・BGM だけの再描画）は保存した決定と cue sheet をそのまま返す。
ワークフローに Activity を足さない（Temporal の履歴は変わらない。`workflow.patched` 不要）。

### 6. 音量と CK61 の再現（INV-154）

`plan_cue_sheet` に曲ごとの `mix_gain_offset_db` を足す（役割ごとの gain + offset、上限は従来どおり `BGM_NARRATION_PRIORITY_MAX_DB`）。
CK61 は所有者の試聴どおり `mix_gain_offset_db = +6`（既定の役割では全 cue が -12 dB に張り付き、プレビューの +6 dB 版と同じ
混合計画になる）。発話中の差 12 LU 以上（`SPEECH_MARGIN_MIN_CENTI_LU`）は**描画の前に**音声だけの stem で実測し、足りなければ
§7 の減衰へ進む。実際に適用した gain と実測の差は `bgm_selections` と BGM 証跡に残す。

### 7. フォールバック（INV-136 を改める。INV-155）

上限つきの梯子を 1 つだけ下りる（無限ループ無し・fal 呼び出し無し・production の成果物は不変）:

1. 選んだ曲で発話中の差を実測 → 足りなければ同じ曲の gain を 3 dB 下げて 1 回だけ再測定
2. まだ足りない・音源が読めない・ライセンスの再検査に落ちた → 次点の承認済み曲で 1 回（最大 2 曲）
3. それでも通らない → **BGM 無し**（理由コードを `bgm_selections.fallback` と証跡に残す）
4. BGM 入りで描画した完成動画のラウドネスが範囲外 → **BGM 無しで 1 回だけ再描画**し、BGM 無しの動画を再検査する。
   BGM 無しでも範囲外なら従来どおり `FinalVideoValidationError`（needs_input。声側の問題）

INV-136 は「BGM 入りの不合格を黙って BGM 無しへ落とさない」だった。改めた後も**黙っては落とさない**: フォールバックは
理由コードつきで記録され、BGM 無しの動画は「BGM 不使用」の明示の PASS（INV-138）と、完成動画の音声に未承認の音源が
含まれないこと（`render_input_hash` に BGM の識別子が無いことと、証跡が `not used` であること）で検査される。
BGM を必須とする設定は持たない（BGM は任意。BGM 無しで合格した動画は投稿できる）。

### 8. 品質ゲートの検査障害と品質問題の区別（INV-156 / INV-157）

判定の語彙は既存の PASS / WARN / REVIEW / BLOCK のまま（`contracts/verdict.py`。状態を足さない）。**RETRY は内部処理**:

- producer の失敗を `InspectionFault`（一時的: I/O・タイムアウト・接続）と `InspectionDefect`（恒久: 契約違反・hash 不一致）に
  分ける。一時的な失敗は gate の中で上限つき（3 回・指数バックオフ・合計 30 秒以内）で再試行する。
- 再試行しても記録できない必須の検査は、これまでどおり REVIEW（**PASS に倒さない**）。ただし理由を
  `QualityReason.INSPECTION_UNAVAILABLE` として品質の REVIEW（`REPORT_NEEDS_REVIEW`）と分けて記録・ログに出す。
- 軽微な問題（説明欄の長さの切り詰め・技術 QA の情報だけの項目など、既存の検査で WARN に当たるもの）は WARN で投稿可。
  WARN にできるのは決定論の検査が明示的に WARN と判定したものだけで、BLOCK を WARN に緩める経路は作らない。
- 権利違反（ライセンス失効・帰属文の不一致）・重大な破損（hash 不一致・ラウドネス範囲外）・二重投稿・必須検査の欠落は
  従来どおり BLOCK / REVIEW で、自動承認しない。

### 9. 投稿経路（INV-158）

通常の Episode Workflow・`UploadWorkflow` の直接起動・手動投稿 API・再試行・過去 Episode の再投稿の**全経路**で、
`AVP_QUALITY_GATE_ENFORCE` が ON なら `upload` Activity の直前に同じ `QualityGateService.evaluate` を通り、PASS / WARN 以外は
YouTube を呼ばない。二重投稿は既存の予約台帳（spent / dispatched）と upload_key で止める。過去の PASS 記録は、
対象の final_video sha256・入力 hash・規則版が一致し鮮度内のときだけ再利用する（INV-126）。

### 10. Feature flag（INV-159）

既存の順序（`AVP_UPLOAD_DESCRIPTION_COMPOSED` → `AVP_QUALITY_GATE_ENFORCE` → `BGM_ENABLED`）を worker の起動時に強制する:
`BGM_ENABLED=true` は説明欄の構成（既存の guard）**と** `AVP_QUALITY_GATE_ENFORCE=true` を要求する。
新しい `BGM_ACQUISITION_ENABLED`（既定 false）は収集ワークフローの worker と Schedule だけを動かし、`BGM_ENABLED=true` を要求する。
OFF なら収集 worker は起動せず、ネットワークに触れない。`GEO_MAPS_ENABLED` は OFF のまま。全 flag OFF の挙動は導入前と同一。

### 11. 監視

構造化ログ（ADR-0059 の契約）に `event=bgm.selection` / `bgm.acquisition` / `bgm.fallback` / `quality.decision` を出す。
項目は episode_id・brand_id・track_id・source・score と理由・ライセンス判定・ダウンロード結果・sha256・適用 gain・
判定・再試行/フォールバックの理由・最終の投稿判断。secret・個人情報・API 応答の原文は出さない。
`domain.quality.hold_rate.recent_hold_alarm(decisions)` は直近 10 件中 5 件以上が品質ゲートで保留されたら警報の
`OperationalAnomaly`（既存の表）を作る。投稿可能率・REVIEW 率・BLOCK 率は `scripts/quality_rates.py`（読み取り専用）で集計する。

## Alternatives

- **人の承認だけを続ける（ADR-0083 §1 の維持）**: 安全だが、所有者が毎回介入する前提が残り依頼の目的に反する。
  機械的に確定できる CC0 に限れば、人の判断が増やす情報は少ない
- **CC BY も初期から自動承認**: 帰属文の正確な生成・説明欄への反映は既存の部品で可能だが、作者表記の揺れ（アップロード者 ≠ 作者）
  と BY-SA の継承条件を機械で確定できない。REVIEW に置き、検証が揃ったら規則の版を上げて解禁する
- **Pixabay / FMA をスクレイピング**: 規約違反。却下
- **選曲を Episode ワークフローの新しい Activity にする**: Temporal の履歴が変わり `workflow.patched` と replay の管理が要る。
  Render の内側なら Activity 契約は不変で、Render の再試行がそのまま選曲の再試行になる
- **選曲時にその場で収集を待つ**: ネットワーク障害が日次の Render を止める。需要だけを積み、次の Episode から使う
- **カタログを `catalog.json` のまま**: 使用履歴・選曲の監査・複数 worker からの同時書き込みに弱い。DDL を 3 表に限って DB へ移す
- **RETRY を Episode の状態に足す**: 状態機械の変更は全経路に波及する。gate の内部の再試行と理由コードで足りる
- **ラウドネス不合格は従来どおり needs_input**: BGM は付加価値で、BGM 無しの安全な版が検査を通るなら止める理由が無い。
  黙って落とすのではなく理由つきで記録する

## Consequences

- 良い: 所有者の介入無しに、機械的に検証できる曲でカタログが育ち、Episode ごとに説明可能な選曲が残る。BGM の故障で
  動画が止まらない。検査系の一時障害で正常な動画が止まらず、止まった理由が「品質」か「検査系」かが記録で分かる
- 悪い / 引き受けた負債:
  - **CC0 の詐称（他人の曲を CC0 で上げる）を機械では見抜けない。** 「本人の作品」の申告・削除候補の除外・Content ID リスクの
    別管理・投稿の private 限定で被害を抑えるだけ。申し立てが来たら `revoked` にして以後の選曲から外す運用が要る
  - 自動取得の対象は当面 Commons だけ。Commons の API 方針が変われば取得は止まる（既存の承認済み曲で制作は続く）
  - 語彙表による要件の推定は粗い。点数の重みは実データで校正していない（初期値）
  - migration 0021 は本番 DB の変更で、所有者の承認が要る。0020 の採番は他の枝と調整が要る
  - フォールバックの梯子のため、BGM 入りの Episode は描画前の音声 stem の測定が最大 4 回増える（音声だけで数秒〜十数秒）
  - 校正は 1 Episode・CK61 の実測が根拠で、他の曲の安全な持ち上げ幅は描画前の実測に頼る
