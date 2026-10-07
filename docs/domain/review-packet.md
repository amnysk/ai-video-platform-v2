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
