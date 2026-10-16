# ADR-0036: 調査用の検索・本文取得は Port の背後の Adapter に閉じ、SSRF 規則を通してだけ取得する

## Status

Accepted (2026-09-29)

## Context

調査（Trend / Evidence）の設計（`docs/Research_機能設計書_v0.1.md`）は、外部の検索結果と外部ページの本文を
台本・企画の根拠に使う。その実装は `claude/research` 系ブランチ（`claude/research-providers`、tip `cfc6ed4`）で
作られたが、`claude/daily-hardening`（本番系統、`ea153c8`）には入っていなかった。

`claude/research` を丸ごと取り込むことはできない。

- 同じブランチに visual style の変更（`SEEDREAM_PROFILE_ID` / `SEEDANCE_PROFILE_ID` の更新、legacy `input_hash` の
  書き換え）が含まれ、merge すると競合として現れないまま既存シーンの fingerprint が変わる。INV-33 の
  「成功済みシーンを再課金しない」と ADR-0034/0035 の 422 対策が後退する。
- migration の revision `0011`〜`0013`、ADR `0030`〜`0033`、INV-27〜31 が本番系統と別の意味で重複する。

そこで Provider Adapter の層（Port・SSRF 規則・本文取得・Fake・YouTube 検索・アーキテクチャ検査）だけを
`cherry-pick -x` で取り込む。この層は `domain.errors` と `infrastructure/youtube/{errors,uploader}.py`
だけに依存し、contracts・DB・migration・production・fal・`paid_job` に触れない。元の設計理由は
`claude/research` 側の ADR-0031 §7/§8 と ADR-0033 §1/§5 に書かれていたが、その番号は本番系統では別の ADR が
使っているため、取り込んだ範囲の決定をこの ADR に記録し直す。

取り込み時点の事実:

- Web 検索・本文取得の client は無かった。HTTP client は httpx のみで、SSRF 対策は存在しなかった。
- YouTube Data API の client（`infrastructure/youtube/uploader.py`）はあるが `search.list` は無かった。
- Codex は `-s read-only` で検索できない。

## Decision

**外部の検索と本文取得は `domain/research/ports.py` の Port（`SearchProvider` / `ContentFetcher`）の背後に置き、
実装は `infrastructure/` の Adapter に閉じる。外部 URL の取得は必ず SSRF 規則を通し、実 Provider はこの ADR では
どの worker にも配線しない。**

### 1. Port と Adapter の分離

- Port（`domain/research/ports.py`、純粋。I/O・HTTP・DNS を持たない。INV-6）:
  `SearchProvider`（`search(query) -> SearchResults`）と `ContentFetcher`（`fetch(url) -> FetchedContent`）。
  Adapter は Port を継承せず構造で満たす（`Protocol`）。
- Adapter は `infrastructure/research/` に置く。YouTube のエンドポイントを書けるのは `infrastructure/youtube/`
  だけ（INV-18 の文字列規則）なので、`YouTubeSearchProvider` は `infrastructure/youtube/search.py` に置く。
- Provider 固有の失敗は `infrastructure/research/errors.py` の `to_domain_error` 1 か所で `domain.errors` の失敗クラスへ写像する
  （docs/failure-policy.md §1）。Research 専用の型（`ResearchUrlNotAllowedError` など）は本番系統の `domain.errors` に無いので、
  遅延 import で基底クラス（`PermanentError` など）へ落ちる。分類（retryable / needs_input / permanent）は変わらない。
- 台本用 LLM が独自に Web 検索する経路は作らない（Codex は `-s read-only` のまま）。

### 2. SSRF 規則（`infrastructure/research/url_guard.py` と `HttpContentFetcher`）

- scheme は http/https のみ。userinfo 付き URL は拒否する。
- ホストを名前解決して**接続先 IP**を検査する（loopback / private / link-local / multicast / reserved /
  unspecified、IPv4-mapped IPv6、`169.254.169.254` などの metadata、`localhost` 系の名前）。
- リダイレクトは自前で追い（最大 5）、**各ホップで同じ検査**をする。接続は検査済み IP へ固定する（DNS rebinding 対策）。
- 接続/読取 timeout、`Content-Length` とストリームの両方で本文サイズ上限、`content-type` の allowlist。
  Cookie・認証ヘッダを送らない。
- PDF は取得するがテキスト化しない（依存を増やさない。ADR-0009）。取得失敗・切り詰め・テキスト無しを
  「本文確認済み」にしない。
- httpx を使ってよいのは `http_fetcher.py` だけ（`tests/architecture/test_research_no_live_network.py`）。

### 3. 実装と Provider 方針（選定は所有者の判断。推測で固定しない）

| Port | 実装 | 状態 |
|---|---|---|
| `SearchProvider` | `FakeSearchProvider`（固定コーパス） | 通常テストの唯一の Provider |
| `SearchProvider`（YouTube） | `YouTubeSearchProvider`（`search.list` → `videos.list` → `channels.list`） | 実装・単体テスト済み（`httpx.MockTransport`）。**未配線** |
| `SearchProvider`（Web） | `NotConfiguredSearchProvider` | **選定未了**。呼ぶと `ResearchProviderNotConfigured`。所有者が選び ADR を足すまで実行しない |
| `ContentFetcher` | `FakeContentFetcher` / `HttpContentFetcher` | `HttpContentFetcher` は §2 付きで実装・単体テスト済み。実ネットワークのテストは無い（INV-18） |

- YouTube の quota 単位（`infrastructure/research/quota_costs.py`: `search.list` 100 units、`videos.list` 1 unit）は
  公開 docs の値で、この repo では検証していない。
- 送った呼び出しは失敗しても課金されたものとして数える（INV-15 の保守的な扱い）。補助呼び出しの失敗は警告に落とし、
  主検索の結果を捨てない。

### 4. 観測値の扱い

- 統計値は観測時刻と組で返す。取れなかった値（例: 非公開の購読者数）は `None` とし、0 と区別する。
- `regionCode` / `relevanceLanguage` は絞り込み・重みづけであり、人気・言語・視聴者層を断定しない。
- `videoDuration` は送らない。4 分未満という条件は Shorts の判定ではない。再生時間は `duration_seconds` として
  事実だけを返す。

### 5. 配線しない

この ADR では worker・compose・env・deploy script・予約台帳（`ProviderCall`）・DB に何も足さない。
`YouTubeSearchProvider` を使う worker は `YOUTUBE_*` を持たず、Web 検索の API キーも無い。
実 Provider の配線（Gateway・Worker・予約台帳への記録・呼び出し件数上限）は、本番系統の採番で別の ADR を書いてから行う。

## Alternatives

- **(a) `claude/research` を丸ごと merge する**: Context のとおり、visual style の変更が競合無しで
  fingerprint と legacy `input_hash` を変え、INV-33・ADR-0034/0035 を後退させる。migration / ADR / INV の番号も
  重複する。却下。
- **(b) Gateway・Worker・DB（元の Tier B）まで同時に取り込む**: contracts・`models.py`・`repositories.py`・
  `paid_job.py` と競合し、migration の採番し直しが要る。本番の 422 対策と同じファイルを触るため、別の作業として
  最新コードの上で移植する。今回は採らない。
- **(c) Web 検索・本文取得を Codex（`--search`）に任せる**: 呼び出し回数・URL・取得内容を Platform が数えも
  検証もできない。却下。
- **(d) 実 Web 検索 Provider を今回選んで配線する**: 費用・quota・利用条件が未確認。推測で外部サービスへ固定しない。
- **(e) `videoDuration=short` で Shorts と判定する**: §4 のとおり判定にならない。却下。

## Consequences

**良い**
- 外部通信の入口が `http_fetcher.py` と `infrastructure/youtube/` に閉じ、アーキテクチャテストで数えられる。
- SSRF 規則が本文取得の唯一の経路に入る。
- 本番系統の production・fal・`paid_job`・scene identity・migration・contracts に変更が無く、422 対策と
  課金安全性に影響しない。

**悪い / 引き受けた負債**
- 取り込んだのは Adapter 層だけで、呼び出し元（Gateway・Worker・Trend・Evidence）が無い。本番では使われないコードとして
  置かれる。
- `claude/research` 側には、同じ Adapter を前提にした Gateway 以降の実装が残っている。その ADR 番号（0030〜0033）と
  INV 番号（27〜31）は本番系統と重複しているため、取り込むときには採番し直し、visual style の変更を切り離す必要がある。
- YouTube の quota 単位は未検証。実行前に所有者が確認する。
- 実 Provider（Web 検索）は未選定。
