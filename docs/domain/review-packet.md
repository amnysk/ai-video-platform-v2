# ReviewPacket と変更案ファイル（JP-MAS-001 §8）

設計 [JP-MAS-001 v2.0](../design/JP-MAS-001-v2.0.md) §8、契約 [intervention.md](./intervention.md)、
[ADR-0041](../decisions/0041-decision-records-and-intervention.md)。型の唯一の宣言元は
`contracts/review_packet.py`。JSON Schema は [review-packet.schema.json](./review-packet.schema.json)
（`python -m contracts.review_packet` で再生成。`tests/contract/test_review_packet_contract.py` が一致を検査）。

## 作り方

`EpisodeReadModel.load(episode_id)`（`infrastructure/intervention/read_model.py`、何も書かない）→
`build_packet(view)`（純粋。JSON 向け）→ `render_markdown(packet)`（人向け。packet だけから作る）。
API は `GET /intervention/episodes/{id}/review-packet?format=json|markdown|html`（担当C）。

## 中身

| 欄 | 内容 |
|---|---|
| `manifest_id` / `manifest_persisted` | 採用 manifest。記録が無ければ null と false（現行の成果物から組んだ） |
| `episode` | phase（`episodes.status`）と制御（mode・control_state・制御版・待機地点。行が無ければ既定値で `persisted=false`） |
| `target_revisions` | manifest の member。採用していない上流から作られた member は `stale` と理由 |
| `decisions` | 判断記録。理由は `rationale.kind`（reported / rule / none …）と本文で、tool 参照（`tool_invocation_refs`）・結果（`result_revision`）とは別欄。保存されていない choose_topic を保存済み候補から導いたものは `derived=true` |
| `script` | 採用中の台本の本文（segment ごと） |
| `evidence` | Episode を参照する research の成果物の参照と照合結果（verdict） |
| `reviews` | コメント・所見・承認・制約。古い版へのコメントはアンカーを動かさず `old_version=true`、旧 manifest の承認は `approval_current=false`（INV-45） |
| `commands` | 状態。`done` は applied のときだけ（受付は完了ではない） |
| `change_sets` | 提案・影響・見積り・提案者の種別（AI の提案を人の編集として示さない） |
| `costs` | 予約台帳・research の見積り（USD）。LLM は `unmeasured`（0 ではない）、見積りの無い呼び出しは件数 |
| `open_reservations` | 送信済み・未精算 / 成否不明 / 未送信 |
| `asset_refs` | bucket / object_key / sha256 **だけ**。signed URL・token を入れない |
| `missing` | 欠けている・確かめていない事柄（manifest 未記録、判断記録の無い revision、stale、旧承認、model は設定値で未検証、token usage 未取得、LLM 費用未計測、照合なし・別版の照合、成否不明の予約、読めない本体） |

全ての文字列は書き出し前にログと同じ安全化（`sanitize_text`）を通す。コメント本文・変更案の影響のように
人・AI が入れた文字列にも秘密や signed URL が混ざりうるため。

## 変更案ファイル（`ChangeProposalFile`）

| 欄 | 内容 |
|---|---|
| `schema_version` | `"1.0"` |
| `episode_id` | 任意。API は URL の Episode と違えば拒否 |
| `base_manifest_id` | 案を作るときに読んだ packet の manifest。各 proposal の `base_manifest_id` はこれか null |
| `proposals` | 1件以上。各要素は `ProposeChangePayload` そのもの（基準 revision は `change.base_revision_id`） |
| `suggested_command_ids` | 任意。作成側の控え（proposals と同じ順・8〜64 文字）。API は自分で決定的な command_id を導く |

予算・Schedule・公開設定を変える欄は無い（未知の欄は拒否 / INV-47）。

## 独立レビュアー（`infrastructure/intervention/reviewer.py`）

`run_reviewer(session_factory, artifact_store, episode_id)`。RoleSpec `REVIEWER`（`contracts/roles.py`）:
LLM を呼ばず、成果物の revision を作らない。採用 manifest が無ければ `ensure_manifest` で現行から作り
（制御行の無い Episode には legacy の行ができる）、その manifest に所見を結び付ける。検査中に採用 manifest が
替われば `ReviewerConflictError`（所見を古い manifest に付けない）。

| check | 重さ | 規則 |
|---|---|---|
| `integrity` | blocking | member の本体が読めない・sha256 が記録と違う・契約違反 |
| `stale_member` | blocking | 採用していない上流から作られた member（読み取りモデルの stale） |
| `narration_budget` | blocking | segment の読み上げ単位が尺の予算を超える（`ScriptLocale`、台本 Activity と同じ式） |
| `storyboard_span` | blocking | storyboard の区間がナレーションに足りない（`check_storyboard_fits_narration`） |
| `final_video` | blocking | technical_qa が無い・不合格、または production manifest が採用中と違う |
| `final_video` | advisory | 完成動画が manifest にまだ無い |
| `evidence` | advisory | 台本照合が無い・別の台本 revision の照合・verdict が passed でない（照合は助言 / ADR-0038） |
| `open_comments` | advisory | 未解決のコメント（レビュアー自身の所見は数えない） |
| `stale_approval` | advisory | 採用中でない manifest への承認（INV-45） |

判定は blocking が1つでもあれば `needs_attention`、無ければ `pass`（候補はその1件だけ、`rationale_kind=rule`）。
入力 snapshot（規則の版・member・stale・コメント・承認・照合）が同じ再実行は既存の記録を返す。
