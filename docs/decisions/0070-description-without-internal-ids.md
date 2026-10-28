# ADR-0070: YouTube description から内部 ID を外す（marker は tags に残す・既定 OFF の flag）

## Status

Accepted (2026-10-28)。設計の出典は `docs/architecture/AI_VIDEO_MULTI_AGENT_DESIGN_v3.md` §5.1 / §5.2（v3 Phase 0
の番号予約 ADR-0070 / INV-90〜92）。**本番での有効化は別の判断**（下記 Consequences）。

## Context

- `build_youtube_metadata` は投稿マーカー（`avpu` + upload key 先頭 24 hex。例 `avpu0f41035d6144c07b16f28327`）を
  tags と **description の最終行**の両方に入れる（ADR-0020 §7）。視聴者に見える description に内部 ID が出ている
- マーカーの目的は二重投稿防止（結果不明時の `find_video_by_marker` 照合。INV-14 / AGENTS.md §9）。
  description から消すだけでは「tags が取れない・編集された・一覧の反映遅延」のときの照合力が下がる
- 既に投稿済みの動画は description にだけ（または両方に）マーカーを持つ。過去動画は変更・削除・再投稿しない
- description は 5000 UTF-8 bytes が上限。現行は本文を切り詰めてマーカー行を残す。クレジット・出典のような
  **必須の帰属テキスト**は今は存在しないが、将来入れたときに本文と同じ扱いで黙って切られてはならない

## Decision

**flag（既定 OFF）が ON のとき、description には投稿マーカーを入れず、内部 ID の lint（契約に1つ）を組み立て時と
`videos.insert` 直前の双方で通し、5000 bytes の予算は必須部分（クレジット・出典）を先に確保して残りを本文に割り当て、
必須部分だけで超えるなら切り詰めずに例外にする。tags のマーカーは変えない。**

- flag は環境変数 `AVP_UPLOAD_DESCRIPTION_WITHOUT_MARKER`（`1` / `true`）。`contracts/upload.py` が名前と読み取りの
  唯一の定義元。Workflow の入力 dataclass には足さない（Temporal 履歴を変えない）。どの compose / env / 既定値でも
  ON にしない
- lint の定義は `contracts/upload.py` に1つ（`avpu[0-9a-f]{24}`、UUID、`minio://` / `s3://` / バケットの object key
  形）。builder（組み立て）と `infrastructure/youtube/metadata.py::insert_body`（送信 body）が同じ関数を import する。
  flag OFF では lint を掛けない（現行の挙動を byte 単位で変えないため。OFF の description はマーカーを含む）
- `find_video_by_marker` / `upload_marker` / 予約・照合のコードは変えない。tags のみの動画と description のみの
  旧動画のどちらも一致する（fake と adapter 契約のテストで固定する）
- 必須部分が単独で 5000 bytes を超える場合は `DescriptionRequiredPartsTooLongError`（`ValueError`）。切り詰めない。
  これを `needs_input` へ写す経路は Upload の Activity 側の別タスク

### 不変条件

INV-90（description に内部 ID なし）・INV-91（tags の marker と旧 description 照合の維持）・INV-92（必須部分を
黙って切らない）を `docs/invariants.md` に追加する。

## Alternatives

- **marker を description と tags の両方から消す**: 却下。照合キーを失い二重投稿の保護がなくなる（設計 §5.2 6）
- **flag を Workflow 入力に足す**: 却下。履歴の形が変わり replay を壊しうる（INV-40 と同じ理由）
- **flag を `Settings` に足す**: 今回は見送り。`contracts/` から infrastructure を import できず、builder は
  Activity の外から呼ばれるため、配線は有効化の判断と同時に別タスクで行う
- **必須部分も本文と同様に切り詰める**: 却下。帰属・出典の欠落は権利・信頼の問題で、保留（人間の判断）が安全
- **lint を送信直前だけに置く**: 却下。受領 Artifact に保存する metadata（送った値の snapshot）が汚染される

## Consequences

- 良い: 視聴者向けの文面から内部 ID が消え、有効化前後で照合・予約・受領の挙動は同じ
- 良い: 禁止パターンと予算の定義が契約に1つ
- 悪い（引き受けた負債）: **tags のみの動画を実 YouTube が `videos.list` で返すこと**は fake でしか確認していない。
  本番で ON にするのは、安全な実機確認ができてから（Phase 0 リスク R2）。tags も外部から見える
- 悪い: ON のとき、受領 `metadata.description` はマーカーを含まない。マーカーは tags から読む
- 悪い: `workers/upload/activities.py::_reserve` の挙動はこの ADR で変えない（予約前の照合の見直しは別タスク）
- 悪い: `DescriptionRequiredPartsTooLongError` を `needs_input` に写す Activity 側の配線は未実装
  （必須部分を渡す呼び出し側もまだ無い。今は builder の引数として用意するだけ）
- 悪い（flag を ON にする前提条件）: `workers/upload/activities.py::_upload` は `build_youtube_metadata` を
  「投稿済み（SPENT）か」の判断**より前**に呼ぶ。ON のとき lint 例外（`ValueError`）は
  `activity_errors.translate_error` で翻訳されず、既存の受領を再利用できたはずの job まで失敗しうる。
  配線タスクで (1) 2つの例外を `needs_input` へ写し、(2) metadata の構築を再利用判断の後ろへ移す。
  この ADR では activities.py を変えない
- 悪い: flag を稼働中に切り替えると、切替前に組んだ metadata の snapshot を持つ受領は marker 入りの description
  を保持しうる。実害はない（`_matching_receipt` は video_id だけで照合する）
- lint の検出範囲: NFKC 正規化とゼロ幅などの書式文字（Cf）の除去のあとで、marker（大文字小文字不問）・UUID・
  32 桁以上の hex（sha256 を含む）・`minio://` / `s3://`・object key を検査する。32 桁以上の hex は sha1 も
  止める（誤検出は保留側に倒れる）。意味を変えた回避（hex の途中に通常文字を挟む等）は検出しない
