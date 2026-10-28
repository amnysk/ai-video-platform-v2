# ADR-0072: BGM 契約とライセンスで門を閉じた選曲（契約・純粋ドメインのみ）

## Status

Proposed（オーナーの承認待ち。番号 0072 と INV-97〜100 は `docs/architecture/v3-phase0-audit.md` の予約）

## Context

設計 v3 §4 は BGM ライブラリを導入する。リスクは「権利の確認できない曲・Shorts で使えない曲を自動採用する」
「帰属必須の指定文を説明欄に載せ損ねる・切り詰める」「取込時の変造・二重登録」「BGM だけの変更が高額な fal 再生成を
誘発する」（監査 R5、設計 T12〜T16・T27）。

現行 Render（ADR-0019）に BGM は無い。**設計 v3 §4.3 は `Remotion` 等へ cue sheet を渡す前提だが、ADR-0019 の描画は
ffmpeg である**。この食い違いは本 ADR では解決せず、Render への結線の枝で ADR-0019 を踏まえて決める
（設計側の記述を現実に合わせる側に倒す）。

## Decision

BGM の語彙と形を `contracts/music.py` の1箇所に定義し、選曲・クレジット・取込検査を I/O も時計も持たない純粋な
`domain/music/` に置く。結線（Render・説明欄・DB）はこの ADR の対象外とする。

- 選曲の除外条件はライセンスの事実だけで、ソフトな優先（直近使用・mood・尺）はそれを覆さない（INV-97）
- 帰属の指定文は原文を取り出し、説明欄に収まらなければ BLOCK を返す。切り詰めない（INV-98）
- 取込は管理者の明示操作。sha256 で重複排除し、変造を拒否する（INV-99）
- cue sheet は production の成果物から独立し、数値は int（INV-100）
- 帰属の指定文は説明欄の sanitize（`contracts/upload.py` の `sanitize_youtube_text`、`<` `>` を全角へ置換）で
  書き換えられてはならない。契約が `<` `>` を拒否し、収まるかは sanitize **後**のバイト数で測る（定義は upload に1つ）。
  このため `contracts/music.py` は `contracts.upload` を import する（INV-100 の「production Artifact に依存しない」は
  Artifact 本体の契約を指し、説明欄の文字規則の共有は許す）
- `credits_for` は `now` を取り、選曲の後に取消・失効した曲と、cue の `license_evidence_hash` が違うものを止める
- cue sheet は start_ms 昇順・同一 scene で重ならない。別 scene どうしの重なり（クロスフェード）は意図的に許す
- 契約を迂回した値（naive な時刻など）は例外で落とさず、理由つきで除外する（fail closed）
- ライセンスの**事実**はコードに持たない。`license_status` は人が証拠（`license_evidence_sha256`）を見て与える値で、
  ここは運ぶだけ。`approved` は証拠 hash と確認日時が無いと作れない

## Alternatives

- **最初から DB・MinIO・Render まで一括で入れる**: 差分が大きくレビューが表層化する（AGENTS.md §3）。契約を先に固め
  て機械検査を付け、結線は個別承認の枝に分ける
- **帰属文を長ければ自動で短縮する**: 指定文の改変はライセンス違反になりうる。短縮ではなく BLOCK
- **直近使用を除外条件にする**: 唯一の合法な曲が選べなくなり、音声抜きの強行へ誘導される。ソフトな並び順に留める
- **cue sheet に dB を float で持つ**: 正準 JSON の hash が揺れ、再実行判定が壊れる（ADR-0019 の `gain_permille` と同じ理由）
- **cue sheet を production Artifact として登録する**: `ArtifactType` の追加が要り、production の input_hash と絡んで
  BGM 変更が fal 再生成を誘発しうる。独立した契約に留める
- **modification_allowed を無視する**: cue sheet はフェード・ducking（改変）を前提とする。設計 v3 §4.2 の項目を
  保守側に倒し、`modification_allowed=false` の曲は選ばない（依頼文の列挙より厳しい。緩める場合は本 ADR を更新）

## Consequences

良い結果:
- 権利の確認できない曲は、他に曲が無くても選ばれない。除外理由が機械可読で、収集依頼の根拠になる
- 結線前でも契約と純粋関数に機械検査が付き、後続の枝が上に積める

悪い結果・引き受けた負債:
- **この ADR は契約だけで、実運用の安全は未達**。Render への結線・説明欄への記載・投稿前 Gate・カタログ DDL・
  MinIO immutable・管理者 CLI は後続で、INV-97〜100 の該当部分は `未検査` と明記してある
- Remotion 前提（設計 v3）と ADR-0019（ffmpeg）の食い違いが残る。mix の実装は ffmpeg の filter で組む想定だが未確認
- `target_relative_level_db` の範囲（-96〜0）は形だけの上下限で、校正した閾値ではない。実素材で校正する（設計 v3 §4.3）
- カタログ DDL は後続の枝で採番する（本 ADR は DDL・新しい `ArtifactType` を足さない）
- cue の重なりの許容範囲（別 scene 間のクロスフェードの最大長など）は実素材での校正後に狭める
- `now` を引数に取るので、呼び出し側が決定論的な時刻を渡す責任を負う
- INV-41〜96 は他の枝の予約。この枝のマージ時に欠番が見える（番号は再利用しない）
