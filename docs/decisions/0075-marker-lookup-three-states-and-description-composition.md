# ADR-0075: マーカー照合の3状態化（UNKNOWN で投稿しない）・タグだけの照合の確認ツール・説明欄の構成

## Status

Accepted (2026-10-29)。設計の出典は `docs/architecture/v3-phase0-audit.md` の R2 / R3 / R10。
**本番での flag 有効化は別の判断**（下記）。

## Context

- `workers/upload/activities.py::_lookup_marker` は、照合 API の失敗（transient / rate limit / quota）を握りつぶして
  `None` を返していた。`_reserve` は round 2 以降（運用者が再投稿を承認した後）に `None` を「動画は無い」と読んで
  新しい予約を作り、`videos.insert` に進む。**照合が失敗しただけで、既に投稿済みの動画があるのに2本目を投稿しうる**
  （INV-14 / INV-15 / ADR-0013 / ADR-0053 の趣旨に反する）。
- adapter の `find_video_by_marker` は、ページ上限（`max_lookup_pages`）で打ち切ったときも、応答の `items` が
  欠けたときも `None` を返す。「読み切って無い」と「読めていない」が区別できない。
- ADR-0070 は description から marker を外す flag を既定 OFF で入れた。外した場合の照合はタグだけに頼るが、
  **実 YouTube がタグのみの動画を `videos.list` で返すか**は実機で未確認（R2）。
- 現行の description は台本の全文（R10）。導入文・補足・参照・クレジットの構成は無い。

## Decision

**照合の結果を `MarkerLookupState = FOUND / NOT_FOUND / UNKNOWN` の型で表し（`contracts/upload.py`）、UNKNOWN は
どの経路でも新しい `videos.insert` の根拠にしない。**

1. `NOT_FOUND` は、uploads playlist を最後のページまで読み切って一致が無かったときだけ。API 例外・timeout・
   quota・auth・応答の形の崩れ・ページ上限での打ち切りは `UNKNOWN`。`_lookup_marker` は設定回数の照合のうち
   **1回でも UNKNOWN があり FOUND が無ければ UNKNOWN**（保守側）。
2. `_reserve` が UNKNOWN を受けたら、**新しいラウンドの予約を作らず**（台帳は abandoned のまま＝未照合）、
   既存の `UploadOutcomeUnknownError`（`NeedsInputError` → job は `needs_input`、非 retry）を送出する。
   新しい Episode 状態・失敗クラス・Activity の入出力・workflow の順序は足さない（replay 互換）。
   運用者がチャンネルを確認して再実行する（`docs/operations/upload-worker.md`）。
3. 既存の `find_video_by_marker`（`str | None`）は互換のため残すが、予約・投稿の判断には使わない。
   新しい port メソッド `lookup_video_by_marker`（`MarkerLookup`）を使う。

4. **タグだけの照合の確認ツール**（`scripts/verify-tags-only-match.py` + `infrastructure/youtube/tags_only_verify.py`）。
   所有者が読み取り専用の OAuth（`youtube.readonly` だけ。他の scope が1つでも付いた token は API を呼ぶ前に拒否）で
   チャンネルの既存の private 動画を GET だけで読み、動画ごとに「description のマーカー照合」と「タグだけの照合」が
   同じ動画を返すかを数える（件数と不一致の video id だけを出す。fixture / dry-run モードあり）。判定の定義
   （`marker_in_tags` / `marker_in_description_lines`）は `contracts/upload.py` に1つで、adapter と共有する。
   **本番で `AVP_UPLOAD_DESCRIPTION_WITHOUT_MARKER` を ON にするのは、所有者がこのツールを実行して 100% 一致
   （比較対象あり・一覧を読み切り）と報告されるまで禁止**。flag は既定 OFF のまま、どの compose / env にも書かない
   （`tests/unit/test_verify_tags_only_match.py` が固定する）。エージェントはこのツールを実 API に対して実行しない。
5. **構成された description**（既定 OFF の flag）。`contracts/upload.py::compose_description` は構造化された入力
   （`DescriptionComposition`: 自然な英語の導入文・任意の補足・出典 `DescriptionReference` の列・クレジット行の列。
   クレジット行は `contracts/music.py` の帰属文 `exact_attribution_text` / `MusicCue.credits_text` をそのまま渡す）から
   導入文 → 補足 → 参照 → クレジット → （marker flag OFF のとき）マーカー行 の順に組む。参照・クレジット・マーカーは
   必須部分で、INV-92 と同じ予算コード（`required_parts_body_budget`）で先に確保する。残りに導入文、補足は
   まるごと入れ、入らない補足は後ろから落とす（導入文は最低 120 bytes 残らなければ BLOCK）。必須部分が入らない・
   参照が必須なのに無い・帰属文が `<` `>` を含む（書き換えない）・内部 ID を含む（INV-90）は
   `DescriptionBlocked(reason)` を返し、`build_youtube_metadata` は `DescriptionBlockedError` を送出する。
   flag は環境変数 `AVP_UPLOAD_DESCRIPTION_COMPOSED`（`contracts/upload.py::UPLOAD_DESCRIPTION_COMPOSITION_ENV`、
   既定 OFF）。**OFF、または `composition` を渡さない呼び出しは従来（台本の全文）と byte 単位で同じ**（golden テスト）。
   Activity は `composition` を渡さない（配線・BGM クレジットの producer は別タスク）。

   AGENTS.md §8 の grep（導入前に実行。`apps workers domain infrastructure contracts docs scripts tests deploy compose*.yaml`）:
   `AVP_UPLOAD_DESCRIPTION_COMPOSED` / `DESCRIPTION_COMPOS*` は **0 件**（読み手 0・書き手 0・無関係 0）。
   導入後は定義元 `contracts/upload.py` の 1 か所が読み手、他は tests / docs（定義の再記述なし）。

## Alternatives

- **UNKNOWN を `TransientError`（retryable）にする**: 却下。retry の度に動画を再ダウンロードし、quota 切れや
  認証失敗では上限まで同じ失敗を繰り返す。結果が不明なものは人が確認する既存の保留（ADR-0020 §4）に揃える。
  一時的な失敗でも保留になる負債は引き受ける（Consequences）
- **`find_video_by_marker` の戻り値を変える / ページ上限で例外にする**: 却下。既存テストの契約
  （`test_find_video_by_marker_not_found_and_bounded`）を変えるため。新メソッドを足す
- **UNKNOWN のとき予約を `unknown`（ADR-0053）へ書き込む**: 見送り。reconciliation の遷移表の変更になる。
  今は予約を作らない（round 2 の前で止める）ので、台帳に誤りは残らない
- **flag を `Settings` に足す / Workflow 入力に足す**: 却下（ADR-0070 と同じ理由。履歴の形を変えない）
- **必須部分（参照・クレジット）が入らないとき導入文や参照を切って通す**: 却下。帰属・出典の欠落は権利の問題。BLOCK
- **最後の1回が NOT_FOUND なら NOT_FOUND にする**: 却下。途中の失敗が最後の成功で打ち消されると、
  部分的に壊れた API の下で「無い」を信じることになる

## Consequences

- 良い: 照合の失敗・部分読みで2本目を投稿する経路が無くなる。結果は型で区別される
- 悪い（負債）: 一時的な API 障害でも、承認済みの再投稿は `needs_input` で止まり、人の再実行が要る
- 悪い: 照合の API 呼び出しが `max_lookup_pages` を超える古いチャンネルでは、常に UNKNOWN になる
  （上限は adapter の設定。引き上げは quota とのトレードオフ）
- 悪い: 構成の入力を作る側（導入文の生成・出典・BGM クレジット）は未実装。今は flag を ON にしても
  Activity は `composition` を渡さないので何も変わらない。導入文が「自然な英語」であることは契約では検査しない
- 悪い: 旧方式（description のみにマーカーがあり、タグに無い）の既存動画は、タグだけの照合では見つからない。
  確認ツールはそれを不一致として報告する。ON にする前に、過去動画の扱い（照合の対象を直近の動画に限る、等）を
  所有者が決める必要がある（ADR-0070 の「過去動画は変更しない」は変わらない）
- 悪い: `_reconcile_by_marker`（bytes 送信後の session 失効）の挙動は元から保留で、変わらない。メッセージだけが
  「見つからなかった」と「確かめられなかった」を区別する

## INV

INV-109（UNKNOWN では新しい投稿をしない）、INV-110（確認ツールは読み取り専用で、flag は既定 OFF のまま）、INV-111（構成 flag OFF の description は従来と byte 単位で同じ）、INV-112（構成された description は必須部分を黙って切らず、内部 ID を出さない）。
