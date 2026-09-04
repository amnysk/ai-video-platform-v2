# ADR-0007: infrastructure層が domain層に依存することを許す

## Status

Accepted (2026-09-04)

## Context

Phase 0 の [architecture/overview.md](../architecture/overview.md) の層依存表は
`infrastructure/ → contracts` のみを許していた。実装が1行も無い段階で書いた表である。

Phase 1 でリポジトリを実装したところ、`tests/architecture/test_layering.py` が
即座に落ちた。理由は2つ:

1. リポジトリは行を **domainのエンティティ**（`Episode` / `Job` / `ArtifactMetadata`）
   へ変換して返す。返り値の型が domain にある以上、import は避けられない
2. リポジトリは状態遷移を **domainの遷移表**（`transition_episode`）に通してから
   書き込む。遷移とDB書き込みを同一トランザクションに収める
   （state-transitions.md 遷移の実装規則3）ためには、ここで表を引く必要がある

回避策として「リポジトリは dict を返し、変換は呼び出し側で行う」も試せるが、
それは型のない境界を1つ増やすだけで、遷移規則の適用場所も散らばる。

**このテストが落ちたこと自体は成功である。** 設計書とコードの食い違いを
実装の初日に機械が通報した。問題は「どちらへ寄せるか」であり、
テストを緩めることではない（AGENTS.md §2）。

## Decision

**層依存表を `infrastructure/ → contracts, domain` に改める。**

domain の純粋性そのものは変えない。`domain/` は引き続き
`apps/` `workers/` `infrastructure/` を import せず、DB・HTTP・Temporal・
ファイルI/O にも触れない（`test_domain_has_no_io_dependencies` が強制する）。
変えるのは**逆向きの1本だけ**である。

INV-6 の本文を同じコミットで更新する。

## Alternatives

**(a) リポジトリのインタフェースを domain に置き、実装を infrastructure に置く
（依存性逆転）** — 教科書的で、依存の向きも保てる。
しかし Phase 1 の実装量に対して抽象が過剰で、Protocol を1つ増やしても
実装は1つしかない。**将来 Protocol が必要になったら導入できる**（後戻り可能）ため、
今は採らない。

**(b) リポジトリが dict / Row を返し、domain への変換を呼び出し側で行う** —
表の文言は守られる。しかし変換コードが Activity と API の両方に重複し、
「同じ真実が2箇所」を自分で作ることになる。却下。

**(c) domain のエンティティを contracts へ移す** — import は contracts で済む。
しかし contracts は「スキーマと共有定数」の置き場であり、
振る舞いを持たない層と定義してある。エンティティを入れると層の意味が濁る。却下。

**(d) テストを緩める / 除外する** — AGENTS.md §2 が明示的に禁止している。却下。

## Consequences

**良い側**
- リポジトリが型付きのdomainエンティティを返せる。API と Activity が同じ型を見る
- 状態遷移とDB書き込みが同一トランザクションに収まる（実装規則3が守れる）
- 依存の向きは依然として一方向であり、`domain` の純粋性は機械検査で守られている

**悪い側 / 引き受けた負債**
- **infrastructure を差し替えるとき domain の型に縛られる。** 別の永続化方式へ
  移る際、domainエンティティの形が事実上のインタフェースになる
- 依存性逆転を後から入れる場合、リポジトリの呼び出し側全部に触ることになる
- 「Phase 0 で書いた表は実装で覆りうる」という前例を作った。
  他のINVも同様に、実装が始まった時点で再検証が要る
