# ADR-0005: Docker Composeでローカル環境を組む

## Status

Accepted (2026-09-04)

## Context

v2は最低でも PostgreSQL / Temporal server / Temporal UI / MinIO /
FastAPI / 複数のworker / Prometheus / Grafana を必要とする。
前身repoは systemd user サービス5本で常駐させていたが、これは
開発マシンの状態に依存し、CIで同じ構成を再現できなかった。

AIエージェントが開発する以上、**「1コマンドで環境が立ち、
壊れたら捨てて作り直せる」**ことが特に重要になる。
エージェントは手作業の環境修復ができない。

## Decision

**Docker Compose をローカル開発とCI統合テストの標準環境とする。**
`docker compose up` で全依存が立ち上がり、`docker compose down -v` で
完全に初期化できる状態を維持する。

## Alternatives

**(a) systemd user サービス継続** — 前身repoの方式。本番常駐には向くが、
開発者/エージェントごとに環境が分岐し、CIで再現できない。却下。

**(b) Kubernetes (kind / minikube)** — 本番がk8sになるなら一貫する。
しかしローカル開発のフィードバックループが遅く、マニフェストの量が
Phase 0の規模に対して過剰。**将来k8sへ移る場合もComposeは残す**方針。却下。

**(c) 各自でローカルインストール（brew / apt）** — 依存の版が揃わない。
Temporal serverの手動セットアップは特に事故が多い。却下。

**(d) devcontainer のみ** — Composeを内包するので排他ではない。
実際 devcontainer は Compose の上に後から足せる。今は不要と判断。

## Consequences

**良い側**
- 環境構築が1コマンド。エージェントが自力で環境を立て直せる
- CIとローカルが同じ定義を共有でき、「自分の環境では動く」が減る
- サービスの版が `compose.yaml` に明記され、レビュー対象になる

**悪い側 / 引き受けた負債**
- **起動が重い。** 8サービス前後になるので、開発マシンのメモリを数GB食う
  → プロファイル分割（`core` / `observability`）で軽い起動経路を用意する
- Composeは本番デプロイの定義ではない。本番構成が別に必要になり、
  **2つの構成が乖離するリスク**を引き受ける（前身repoの事故の形そのもの）
  → 環境差分は環境変数に閉じ込め、サービス構成自体は同じ形を保つ規律で対処する
- ビルドキャッシュが効かないとCIが遅くなる。レイヤ設計に注意が要る
