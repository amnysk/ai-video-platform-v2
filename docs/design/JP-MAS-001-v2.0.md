# Japan’s Past：介入可能なマルチエージェント動画制作システム設計

設計ID：JP-MAS-001
設計版：2.0／2026年10月7日（日本時間）
対象：ai-video-platform-v2
状態：設計。リポジトリ・本番の実査、実装、動画生成、公開は行っていない。
対応する実装指示書：Claude_Code_Multi_Agent_Implementation_Instructions_2026-10.md
この文書は「システムが何を満たすか」を定める。Claude Codeの作業分担・実装順・報告方法は別文書に定める。

## 1. 目的と基本方針

ユーザーが「AIが何を調べ、どの案を比較し、なぜこの台本と映像を選んだのか」を確認でき、ユーザーと対話するAIアシスタントが制作途中に修正を提案し、許可された変更を安全に反映できるようにする。

中心に置くのは、エージェントの会話ではなく、保存された制作成果物と判断記録。その上に、専門ごとのエージェント、介入手段、検索、評価を追加する。

必須の性質：
1. 判断から入力・資料・生成結果へたどれる。
2. 制作中でも、完成後でも、特定の版と区間にコメントできる。
3. 修正は新しい版として残り、以前の版と比較できる。
4. 停止・変更・再開によって成功済み素材を失わず、不要な再課金や二重投稿を防ぐ。
5. エージェント、モデル、ツール、画面、動画形式が増えても保存形式と介入契約を維持できる。
6. 日次実行を維持しながら、任意のEpisodeで人が介入できる。

「AIがどのように考えたか」は、選択肢・判断理由の構造化要約・根拠・実行記録で説明する。これは観測できる判断記録であり、モデル内部の思考を完全に再現した記録ではない。AIが述べた理由と、実際のツール参照・実行結果を別々に保存し、説明の裏付けを確認できるようにする。

## 2. 現状への接続と維持する条件

共有済みの流れはTopic Planner → Script → Storyboard → Production → Render → Upload。Research Gateway、Temporal、シーン単位fallback、成功素材の再利用、style_id、予約台帳を前提とする。実装状況は導入時に確認する。

今回の追加：
- 企画と台本の前後に、構造化された判断記録とレビュー地点。
- 各生成結果に、採用・不採用・差し替えの履歴。
- 編集前後に、映像・音声・字幕を使うレビュー。
- 全工程に、版を指定する介入API。
- 公開後に、実績を次の企画へつなぐLesson。

維持条件：
- Shorts専用値をハードコードしない。format_profileを使う。
- 既存の422/file_download_error対策、paid_job、scene identity、予算・投稿の冪等性を維持する。
- 1本1,000円、月30,000円にはLLM、検索、レビュー、失敗再試行も含める。
- Schedule、公開設定、予算上限は制作AIの判断だけで変更しない。
- private uploadと一般公開は別イベントとして記録する。
- OpenSearchの検索障害で、正本の制作データや介入履歴を失わない。

## 3. 制作現場に対応する役割

現場の制作では、企画・調査・演出・編集に異なる責任がある。特に編集では、撮影済み素材を見て構成やナレーションを調整する。[S1]

| 論理的な役割 | 主な責任 | 必須成果物 |
|---|---|---|
| プロデューサー | 対象視聴者、シリーズ、企画採否、費用配分 | CreativeBrief、DecisionRecord |
| リサーチャー／考証 | 主張の裏付け、留保、素材利用条件 | EvidencePack、claim_id対応 |
| ライター | 冒頭、説明、答え、自然な英語 | ScriptRevision |
| ディレクター／美術 | シーンの役割と映像化手段 | ShotPlanRevision |
| 編集／レビュアー | 実素材の意味・音声・字幕・完成版の品質 | Review、EditPatch |
| アナリスト | 公開後の観測と次回の仮説 | MeasurementSnapshot、LessonProposal |

初期はプロデューサー、ライター兼ディレクター、独立レビュアーの3実行役でよい。考証と完成品のレビューは同じ実行役でも別タスク・別入力として扱う。実行プロセスやモデル数を役割数に合わせて増やす必要はない。

役割は自由な会話で共有DBを直接書き換えず、保存された入力を読み、schemaに従った成果物・提案を返す。採用する権限は役割と運転ポリシーで決める。

## 4. 介入地点と運転モード

| 介入地点 | 確認できるもの | 介入例 | 主な後続への影響 |
|---|---|---|---|
| 企画採用前 | 候補、対象、資料、過去動画との関係 | 別企画にする、問いを絞る | 台本・演出 |
| 台本確定前 | 原文、根拠、英語、予定尺 | 一文修正、史実の留保追加 | 音声・字幕・演出 |
| 有料素材生成前 | 絵コンテ、style、素材案、費用見積もり | 図解へ変更、シーン差し替え | 対象の生成素材 |
| 素材取得後 | 実際の画像・動画・音声 | 素材の不採用、別案提案 | 編集・レンダリング |
| 完成品確定前 | プレビュー、字幕、レビュー | 特定区間の編集、音量修正 | 完成版・Upload |
| 公開後の振り返り | 指標、区間、コメント、Lesson | 仮説を変更、次回の実験を決める | 次のEpisode |

運転モード：
- 自動＋介入可能：既存条件を満たす範囲で自動進行。任意のEpisodeへ停止・コメント・変更を送れる。
- 指定地点でレビュー：設定した地点だけWAITING_REVIEWで待つ。
- 共同制作：主要な地点で待ち、人と対話して版を確定する。

既存の自動実行の初期設定を、今回の設計だけで一律に待機モードへ変えない。新方式の試験Episodeはレビューまたは共同制作を選べる。モード・待機地点・タイムアウト時の方針はpolicy_versionに記録する。

待機期限を過ぎた場合、勝手に承認したものとして進めない。「待機継続」「保留終了」「既に許可された旧版を採用」等の明示ポリシーで決める。日次Scheduleで待機中のEpisodeが次回発火に与える影響も表示する。overlap禁止とdaily limitを維持し、Schedule停止と個別Episode停止を別の操作にする。

## 5. データを残す3層

| 層 | 内容 | 保存の原則 |
|---|---|---|
| 制作成果物 | 企画、台本、絵コンテ、素材、字幕、編集、完成動画 | 不変revisionとして保存。採用版を参照で指定 |
| 判断・介入履歴 | 候補、根拠、採否、人のコメント、修正、承認 | 追記型。actor、対象版、前後関係を保存 |
| 実行・評価記録 | LLM・tool呼び出し、ジョブ、費用、レビュー、視聴指標 | 実行結果と測定時点を保存。制作判断へ接続 |

正本：
- 既存RDB：ID、関係、状態、版、判断、コマンド、費用。
- 既存オブジェクトストレージ：大きい入出力、資料の必要箇所、画像・音声・動画、プレビュー、書き出し。
- Temporal：進行、待機、再試行、メッセージ履歴。
- OpenSearch：検索・ログの投影。RDBと素材保存先から再構築可能にする。

既存テーブルを再利用できる場合は再利用する。全面イベントソーシング、新しいDBやメッセージブローカーの導入は必須にしない。制作に必要な状態と、追記型の記録を同じDBで整合させる。

## 6. データ契約

以下は論理エンティティ。物理テーブルの数や命名は現行schemaとの適合で決める。

| エンティティ | 主な項目・関係 |
|---|---|
| Episode／Workspace scope | episode_id、所有範囲、mode、現在のphase、active_revision_set |
| ArtifactRevision | artifact_id、revision_id、kind、parent_revision_id、schema_version、content_hash、保存参照 |
| RevisionDependency | 上流revision_id、下流revision_id、依存種別、影響範囲 |
| AgentRun | agent_run_id、role、model識別子、prompt_version、context_snapshot_id、予算・時間・結果 |
| ContextSnapshot | 実際に渡した入力、参照した戦略・Lesson・成果物の固定版 |
| ToolInvocation | tool_call_id、agent_run_id、許可判定、引数・結果参照、時刻、費用、error |
| DecisionRecord | decision_id、候補、選択、短い理由、根拠参照、留保、対象revision |
| EvidenceItem | source_id、URL／資料ID、取得時刻、該当箇所、支持するclaim、利用条件 |
| Review／Comment | 対象revision、シーン／一文／時間区間、観測、問題、提案、解決状態 |
| InterventionCommand | command_id、requester、executor、type、expected版、payload、受付・適用結果 |
| ChangeSet／Variant | base_revision、変更案、影響範囲、見積費用、適用版、選択状態 |
| ReviewCheckpoint | 地点、対象manifest、必要条件、状態、判断者、判断イベント |
| Measurement／Lesson | 公開時刻、測定条件、母数、欠測、観測と仮説、検証状態 |
| Outbox／Execution receipt | 未配送の通知、配送済み、適用結果、再送・復旧用の参照 |

共通項目：
- schema_version、created_at、actor_type／actor_id、correlation_id。
- episode_id、branch_id、revision_idまたは対象manifest_id。
- 元のユーザー指示やコメントの参照。秘密情報は除く。
- 人／AI／システムによる変更の区別。AIの提案を人の直接編集として記録しない。

revision_idは不変、active_revision_setは更新可能なポインタ。作成時刻だけで因果関係を判断せず、Episode内のevent_seqとparent／causation_idで前後をたどる。

## 7. 判断記録の内容

DecisionRecordに残す内容：
- 判断する問い。
- 実際に提示された候補と候補ごとの利点・懸念。
- 採用候補と不採用理由。
- 参照した資料、過去Episode、Lesson、tool_call_id。
- 予算・provider・形式等の制約。
- 未確定事項と、追加調査が必要な点。
- 適用した台本・演出・素材のrevision。
- AI自身の評価と、機械検査／視聴者データの区別。
- 人が上書きした場合、その指示と前後の差分。

モデルが自己申告した理由はreported_rationaleとして保存する。参照したと述べた資料が実入力に存在するかを照合できるようにする。採用後に説明を生成した場合はpost_hoc_summaryと明記する。

候補やスコアを後から架空に埋めない。候補が一つなら一つと記録する。数値評価は自己評価か実測かを明記し、自己評価の点数を視聴者の好みの実測値にしない。

例（架空の記録。実動画の観測ではない）：

~~~json
{
  "schema_version": "1.0",
  "decision_type": "choose_visual_method",
  "target": {"scene_id": "s3", "shot_plan_revision": "shot-r4"},
  "question": "印刷の色が重なる工程をどう見せるか",
  "candidates": [
    {"id": "a", "method": "generated_video", "concern": "工程が誤って描かれる可能性"},
    {"id": "b", "method": "controlled_diagram", "benefit": "色と順序を編集で管理できる"}
  ],
  "selected_candidate": "b",
  "reported_rationale": "工程の理解を優先し、色と順序を管理できる図解を選ぶ",
  "evidence_refs": ["evidence-printmaking"],
  "input_snapshot_ref": "context-r2",
  "tool_invocation_refs": ["tool-read-evidence-1"],
  "uncertainties": ["完成プレビューの分かりやすさは未評価"],
  "result_artifact_revision": "shot-r5"
}
~~~

LLMのアプリケーションから見えるrequest／response、tool callを必要な範囲で保存する。認証情報を除去し、資料全文の保存は利用条件を確認する。token usage、provider request ID、model識別子、生成設定を残すが、同じ設定による再生成で完全に同じ出力が得られる保証はしない。

## 8. 私とあなたが介入するための入り口

### ユーザー向けのEpisodeレビュー
初期は既存管理画面、簡易画面、または書き出しで、次を確認できるようにする。
- 現在の工程、待機理由、進行中ジョブ、費用。
- 企画・台本・絵コンテ・完成映像。
- 採用案と不採用案、その理由と根拠。
- 版の差分、未解決コメント、変更の影響範囲。
- 現在採用している版と、比較中の別案。

操作：
コメント、停止要求、修正提案、差分確認、採用／却下、再開。巨大な新規管理画面を初期要件にせず、APIとReviewPacketを先に整える。

### 対話するAIアシスタント向け
このChatGPTが将来自動的に本番へ接続できるとは仮定しない。接続前はReviewPacketを読み、変更案を作り、ユーザーが管理手段から取り込む経路を用意する。

ReviewPacketには、manifest_id、Episode状態、対象revision、判断記録、台本、根拠、レビュー、費用、素材参照、欠測・未確認事項を含める。JSONを機械向け、Markdownを人向けに出力する。完成素材は権限付き参照または取り出せる保存済みファイルとして提供する。

将来の接続は同じAPIのadapterとして追加する。
- read_episode / read_decisions / compare_revisions / export_review_packet
- propose_change / request_hold / get_command_status
- 権限とユーザー指示の範囲内のapply_change / resume_episode

MCP等を使う場合も、DBへの直接書き込みより既存の検証を通る介入APIを使う。AIアシスタントが独自に予算上限、Schedule、公開可視性を変える権限は持たせない。操作ごとの権限と、ユーザーから授けられた範囲を記録する。権限が既に与えられた操作には毎回確認を要求しない。

### コメントのアンカー
台本の一文にはsegment_id、映像にはscene_id・asset_id・time_range、全体にはmanifest_idを付ける。版が変わっても元コメントの対象を保持する。移動できないコメントは古い版のコメントとして表示し、誤った区間へ自動移動しない。

## 9. 介入の適用と競合

変更は直接上書きせず、次の順で扱う。
1. 対象版を指定してコメント／ChangeSetを作る。
2. 依存関係から影響範囲を計算し、必要な生成・再確認・追加費用を見積もる。
3. 権限、版の一致、予算、品質条件を検証する。
4. 変更を新revisionとして適用する。
5. active_revision_setを更新し、古い下流結果をstaleにする。
6. 必要な工程だけ再実行し、結果を保存する。

コマンドにはcommand_idとexpected_episode_revision／expected_target_revisionを含める。更新が競合した場合は409等で競合を返し、差分を再取得する。ユーザーの修正をAIが黙って消さない。

AIへ「この文章は保持」「このシーンは変更しない」と指示した場合、対象範囲をuser_constraintとして保存する。後続の提案が抵触する場合は別案か明示的な変更提案にする。自由記述だけでなく検証できる対象IDも持たせる。

コメントと適用命令は別。コメントしただけで有料再生成を始めない。ChangeSet適用が既に許可された自動修正ポリシー内なら実行できる。範囲外の場合は未適用案として残す。

承認や品質合格は、台本・素材・字幕・編集・形式設定を含む確定manifestに結び付ける。manifestを変更した場合、影響するレビューと合格を再確認する。

## 10. 停止・再開の意味

Episodeの制作phaseとcontrol_stateは分ける。
- phase：planning、scripting、production、editing、ready等。
- control_state：RUNNING、HOLD_REQUESTED、HELD、WAITING_REVIEW、CANCEL_REQUESTED等。

request_holdは新しい生成・投稿の開始を抑制する制作制御。受付時点ではHOLD_REQUESTEDとし、dispatchの競合が整理され、新しい副作用が開始しない境界に到達したらHELDを返す。

停止中も可能にする処理：
- 読み取り、コメント、変更案作成。
- 送信済みprovider jobの状態照会、結果保存、費用の精算。
- 停止前後の操作履歴の記録。

開始済みジョブは停止後も完了・課金され得る。cancelを要求できても、providerが停止できたことと費用が返ることは別に扱う。未送信と送信済みと成否不明を区別し、安易に予約を解放しない。

gatewayで新規副作用の開始許可とcontrol_stateを照合する。既に発行された開始許可・送信中処理を追跡する。HTTP送信とDB更新は完全に同時にはできないため、停止受付だけで「以降の追加課金ゼロ」と保証しない。HELD到達前の進行中処理を表示する。

台本・シーンを変更した後に旧版の生成結果が届いても、元のrevisionへ保存する。新しい採用版へ自動で混入させず、再利用できる場合だけ採用する。

再開時は、新しいmanifest、staleな依存結果、進行中・成否不明ジョブ、残予算を確認し、必要箇所から再開する。過去版への復元も新しい採用イベントとして残す。既に発生した課金や一般公開をデータ復元で取り消したことにはしない。

## 11. Temporalとの境界と配送の整合性

LLM、検索、DB、素材取得はActivity。進行とレビュー待機はWorkflow。Workflow内で外部I/Oを直接実行しない。[S2]

読み取りはDBの投影とQuery、変更通知はUpdateまたはSignalを使う。利用中SDK・Serverの対応を確認して決める。非同期通知では、受け付けと反映をcommand_idで確認する。TemporalのUpdateもAcceptedとCompletedは別の段階として扱われる。[S3]

提案する整合性の方式：
- APIがコマンドをDBへ保存し、同一transactionでoutboxを保存する。
- dispatcherが通知する。配送は重複を想定する。
- Workflowはcommand_idの通知を受け、Activity経由で内容を確認する。
- Episode内の適用を直列化し、DBの期待版とunique制約で一度の論理変更にする。
- DBで適用済みだがWorkflowが結果を受け取れなかった場合、同じcommand_idの再確認で既存結果を返す。
- 状態と判断記録の保存をtransaction境界にまとめ、途中終了で理由だけ残る／版だけ変わる不整合を避ける。

commandの業務状態：
RECEIVED → VALIDATED → APPLYING → APPLIED、またはCONFLICT／REJECTED／FAILED。
RECEIVEDを「修正完了」と表示しない。外部生成を伴う場合、コマンド適用と完成動画の生成完了も分ける。

制作のHELDはアプリケーション内の待機。Temporal運用機能のWorkflow Pauseとは区別する。公式のWorkflow Pauseは動作中Activityを中断せず、pause中はQueries／Updatesが拒否されるため、共同編集の通常手段として全面依存しない。[S4]

稼働中Workflowの互換性を維持するversioning／patchingを使う。実装済みのMessage handlerやsignalを調査し、重複した別制御面を増やさない。

## 12. 別案の比較と依存関係

Variantを使い、同じ企画から台本A／B、冒頭A／B、編集A／Bを比較できる。初期は台本とShotPlanの比較に限定してもよい。

- Variantはbase_revisionを参照する。
- 共通素材は複製せず、asset_idとhashで再利用する。
- 未採用案は本番Uploadへ流さない。
- 両案の有料生成は、両案分の予算が許可された場合だけ行う。
- 採用時に選択理由を記録し、非採用案のデータを消さない。

代表的な依存：
- 台本の音声部分変更 → TTS・字幕・タイムライン・最終Render。
- 史実の変更 → Evidence、台本、対応する映像・レビュー。
- 映像のみ差し替え → 対象素材、編集、必要な字幕配置確認、Render。
- BGM・音量変更 → audio mix、完成品レビュー、Render。
- 形式設定変更 → 寸法・字幕レイアウト・Render・投稿設定検証。

素材の再生成範囲と、品質を再確認する範囲は別。変更によってレビューが必要な範囲が広がっても、全素材の再生成を要求しない。

履歴を読み直すこと、保存済み成果物で再レンダリングすること、LLM／providerを再呼び出しすることを別操作にする。「replay」の一語で区別しない。再生成は新しいrunと費用として記録する。

## 13. 拡張の契約

| 拡張対象 | 準備する境界 | 初期の実装範囲 |
|---|---|---|
| エージェント追加 | RoleSpec：入力・出力schema、許可tool、予算、終了条件 | 3実行役 |
| 検索・生成provider | Tool／Provider adapterとcapability | 現行providerを包む |
| 動画形式 | FormatProfileと配信設定 | 現行形式＋追加可能なschema |
| UI／ChatGPT接続 | 同じread・command API、ReviewPacket | 書き出しと取り込み |
| 判断基準 | QualityPolicy／InterventionPolicyの版 | 既存条件＋レビュー地点 |
| 記憶・分析 | Lessonの適用範囲とMeasurement schema | 公開後の観測を保存 |
| 保存先・検索 | ArtifactStorage interface、検索投影 | 現行保存先＋OpenSearch |

RoleSpecやpluginは、任意コードを自動ダウンロードして実行する仕組みにはしない。構成変更はソース管理された設定と検証を通す。schema_versionとmigrationを持ち、古いEpisodeも読めるようにする。

モデル・プロンプト・agent構成・strategy_version・policy_version・tool schema・コードcommitをRunManifestに固定する。更新は原則新しいrunから適用する。既存Episodeへの変更は介入イベントとして残す。

この境界を設けることと、全ての拡張を最初に実装することは別。初期はAPI／保存契約とadapterの入り口を整える。

## 14. 保持・検索・復旧

- 制作成果物、判断、介入、採用履歴、測定は永続保存対象。初期に自動削除を設定しない。
- 大きい元素材、未採用案、プレビュー、運用ログの保持期間は別設定にする。容量と費用を見て選べるようにする。
- providerの一時URLだけを保存しない。保存可能な生成素材は管理下の保存先へ取り込み、checksumと取得状態を記録する。
- source URL、取得日時、必要箇所、取得時点のhashを保存する。外部ページの全文保存は利用条件と必要性を考慮する。
- signed URLは読み取り時に発行する。期限切れのURLを正本としない。
- バックアップと復元で、RDBの参照とオブジェクトの整合性を検査する。
- OpenSearchは検索用に再構築できる。未投影や遅延を表示し、直接のEpisode読み取りは正本から可能にする。
- 人が削除を選んだ場合、素材参照・履歴・バックアップへの影響を明確にする。履歴の存在を「ファイルが必ず再生できる」ことと混同しない。
- 認証token等は保存前に除去する。権限付き素材のリンクを公開ログへ出さない。

初期に保存する全項目を巨大な全文ログへ入れず、ログから正本への参照を持たせる。

## 15. OpenSearchと監視

すべての新処理に既存の共通ログ契約を適用する。

相関情報：
episode_id、branch_id、manifest_id、artifact_revision、agent_run_id、decision_id、command_id、actor_type、workflow_id、run_id、trace_id。

イベント：
decision_recorded、comment_added、hold_requested、hold_effective、change_proposed、change_applied、change_conflicted、review_invalidated、review_resolved、checkpoint_waiting、checkpoint_released、variant_selected、resume_requested、resume_completed。

agent／toolの共通情報：
model、prompt_version、tool_name、tool_call_id、duration、token_usage、estimated_cost、actual_cost、retry_count、outcome。

監視対象：
- 判断記録の欠落、入力snapshotの欠落。
- 介入配送の滞留、RECEIVEDからAPPLIEDまでの時間。
- 長時間のHOLD_REQUESTED、成否不明ジョブ。
- staleなレビュー／manifestの使用。
- 予算の不一致、重複provider送信、重複投稿。
- 検索投影の遅延、保存済み素材の欠落。

必須の判断・変更記録が保存できなければ、新方式の有料生成・投稿は開始しない。OpenSearchへの投影だけが失敗した場合、正本とoutboxが保存されていれば再配送する。記録の正本と運用ログの転送を区別する。

## 16. 公開後の学習と介入

Episode、採用manifest、公開video_id、公開日時、48時間／7日／28日の測定を結び付ける。private uploadのままなら一般視聴者の観測は待機する。

Lessonは「観測」「仮説」「別の説明」「次の検証」「適用範囲」「提案／検証中／採用／撤回」を持つ。人とAIアシスタントがLessonにコメントし、次回の仮説を変更できる。

維持率の落ち込みとあるカットが重なっても原因とは断定しない。米国と年齢が別集計なら米国20代へ届いたことを断定しない。欠測はゼロで埋めない。[S5]

自己批評は制作上の改善候補。モデルの再学習を最初から行わず、検証されたLessonを検索して次回のContextSnapshotへ入れる。

## 17. 初期の完成範囲と検証条件

初期の完成範囲：
- 判断記録・context snapshot・不変revision・依存関係。
- 読み取りとReviewPacket書き出し。
- コメント、停止、変更案、差分・費用確認、適用、再開。
- 権限、期待版、command_idによる重複防止。
- 新方式を選べる設定と、従来の自動経路。
- 正本保存、OpenSearchへの投影、バックアップの検証。
- 高額な再生成や本番投稿を使わない隔離環境での動作確認。

必須の検証：
1. 企画採用理由から実際の入力・tool結果・資料・成果物へたどれる。
2. 台本を修正すると新revisionができ、古い版とコメントが残る。
3. 同じcommand_idの再送・復旧で変更と生成が増えない。
4. 同じ版を人とAIが同時変更すると競合を返す。
5. 停止の受付と有効化が区別され、有効化後に新規生成・投稿を始めない。
6. 停止前に送信したジョブの完了・課金・旧版素材を正しく扱う。
7. 修正後、成功済みの無関係な素材は再生成しない。
8. 変更したmanifestへの古い合格判定を流用しない。
9. DB適用後にWorkflowが落ちても、再確認で一度の変更に戻る。
10. OpenSearch停止から復旧・再投影でき、正本は読める。
11. バックアップ復元後も判断・版・素材参照が整合する。
12. 人／AIの権限を超える予算・Schedule・公開変更を拒否する。
13. 旧方式、既存fallback、Shorts以外の形式を扱う契約が維持される。

本番へ入れる際は、現行契約・replay互換性・既存運用を確認する。この設計書だけで実装済み・デプロイ済みとは扱わない。

## 18. 介入例：浮世絵動画

前の机上試行で選んだ「浮世絵の制作チーム」を例にする。Metの資料で分業を確認したが、以下の変更と映像問題は説明用の仮定で、実動画を見た結果ではない。[S6]

1. AIが企画候補3案と採否を保存する。
2. ユーザーが「冒頭で完成作品をもっと早く見せたい」とコメントする。
3. AIアシスタントがScript／ShotPlanの対象版を読み、冒頭の入れ替えをChangeSetとして提案する。
4. 依存関係から、既存画像は再利用でき、音声と字幕・Renderが影響を受けることを示す。
5. ユーザーまたは既に許可されたポリシーが変更を採用する。
6. 新revisionを作り、必要な部分だけ再実行する。
7. 完成レビューで「00:20の色重ねが分かりにくい」と指摘する。
8. agentが図解の改善を提案し、変更前後を比較する。
9. 公開後、当該区間の成績を確認し、次回の仮説を作る。

この一連の履歴を、Episode画面／ReviewPacketから読めることが設計の目標。

## 参考資料

S1. ScreenSkills, Edit producer (Unscripted TV)
https://www.screenskills.com/job-profiles/roles/edit-producer/

S2. Temporal, Workflow Definition
https://docs.temporal.io/workflow-definition

S3. Temporal, Handling Signals, Queries, & Updates／Python SDK
https://docs.temporal.io/handling-messages
https://docs.temporal.io/develop/python/workflows/message-passing

S4. Temporal, Workflow Pause
https://docs.temporal.io/encyclopedia/workflow/workflow-pause

S5. YouTube Analytics API — Metrics／Channel Reports
https://developers.google.com/youtube/analytics/metrics
https://developers.google.com/youtube/analytics/channel_reports

S6. The Metropolitan Museum of Art, Woodblock Prints in the Ukiyo-e Style
https://www.metmuseum.org/essays/woodblock-prints-in-the-ukiyo-e-style

既存方針：Japans_Past_Strategy_Roadmap_2026-09.md。今回の前段で全文確認したシリーズ・測定・通常動画拡張の方針を引き継ぐ。
