# ADR-0004: FastAPIをAPI層に採用する

## Status

Accepted (2026-09-04)

## Context

将来的にWeb UI（Next.js）から企画・制作・再実行・投稿・分析を操作する。
そのためのHTTP APIが要る。前身repoにも `server.py`（約910行）があったが、
HTTPハンドラの中で重い処理を同期実行しており、リクエストがタイムアウトしたり、
サーバ再起動で処理が失われたりした。

またこのシステムはAIエージェントが継続開発する。**型と契約が実行時に検証され、
スキーマが自動生成される**ことの価値が通常より高い ── エージェントは
ドキュメントよりスキーマを正確に読む。

## Decision

**FastAPI を API 層に採用する。** ハンドラの責務は
「検証 → 永続化 → Temporal workflow の start / signal / query → 202」に限定し、
重い処理を同期実行しない（INV-16）。

## Alternatives

**(a) Flask / 素のASGI** — 軽い。しかしリクエスト/レスポンススキーマの検証と
OpenAPI生成を自作することになる。エージェント開発における型の価値を捨てる。却下。

**(b) Django + DRF** — admin画面が最初から手に入るのは魅力。しかしORMと
プロジェクト構造が強く、`domain/` をフレームワーク非依存に保つ（INV-6）のが難しくなる。
またasync対応が後付けで、Temporalクライアントとの相性が悪い。却下。

**(c) Next.js の API Routes に寄せて Python API を持たない** — 構成は減る。
しかし Temporal の Python SDK と domain ロジックがPythonにあるので、
TypeScript側から呼ぶには結局RPC層が要る。却下。

**(d) gRPC** — 型は最も強い。しかしブラウザから直接叩けず、
grpc-web かゲートウェイが要る。UI要件に対して過剰。却下。

## Consequences

**良い側**
- Pydanticによるリクエスト/レスポンス検証が `contracts/` と同じ型で書ける
- OpenAPIスキーマが自動生成され、Next.js側の型を生成できる
- asyncネイティブなので Temporal client と自然に噛み合う
- ハンドラが薄いので、APIのテストが軽い

**悪い側 / 引き受けた負債**
- **「重い処理を書かない」規律はフレームワークが強制しない。**
  素朴に書くとハンドラ内で動画生成を待ってしまう。
  architecture test（`tests/architecture/test_api_no_heavy_work.py`）で
  検出する必要があり、これはまだ未実装
- 全操作が非同期（202 + ポーリング/購読）になるので、UIの実装が
  同期APIより複雑になる。UI側に進捗表示の責務が増える
- Pydanticのバージョン移行が過去に破壊的だった。バージョンを固定する
