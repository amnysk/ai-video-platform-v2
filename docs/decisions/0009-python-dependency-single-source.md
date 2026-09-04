# ADR-0009: Python依存の宣言元を pyproject.toml ひとつにする

## Status

Accepted (2026-09-04)

## Context

Phase 1 の `Dockerfile` は、レイヤキャッシュを効かせる目的で依存を
**ベタ書きのリテラル**で持っていた。同じリストが `pyproject.toml` にもあり、
**同じ真実が2箇所**にあった。

これは机上のリスクではなく、実際に発火した。psycopg2 を追加する際、
`pyproject.toml` と `Dockerfile` の**両方**を手で直す必要があった。
今回は両方直されたが、片方だけ直せば:

- pyproject だけ直す → ローカルとCIは緑、**Dockerだけ落ちる**
- Dockerfile だけ直す → Dockerは動く、**ローカルとCIだけ落ちる**

どちらも「変更しなかったほうの読み手」が壊れる形で、差分レビューでは見つからない。
そしてこの一致を検査する機械は**1つも存在しなかった**。

AGENTS.md §8 の文言は Python コード内の値・列挙を対象としており、
pip 依存リストはその文言の外にあった。**ルール側の穴**でもある。

## Decision

**Python依存の宣言元は `pyproject.toml` ひとつとする。**

- `Dockerfile` はパッケージ名を書かず `pip install -c constraints.txt .` で取り込む
- **版の固定**は `constraints.txt`（`python:3.13-slim` 上で解決した全推移的依存）に置く。
  宣言（pyproject）と固定（constraints）は役割が違うので、これは二重管理ではない
- `.dockerignore` を追加し、ビルドコンテキストから `.venv`（約250MB）等を除く
- 機械検査を置く: `tests/contract/test_dependency_single_source.py`
  - Dockerfile の `pip install` 行に pyproject の依存名が現れないこと
  - Dockerfile が `pip install .` でプロジェクト自身を入れていること
  - 宣言された依存がすべて `constraints.txt` で固定されていること

AGENTS.md §8 の適用範囲に「Python パッケージ依存」を明記する。

## Alternatives

**(a) 二重管理のまま、一致を検査するテストだけ足す** — 変更が小さい。
しかし「2箇所を人が同期し続ける」構造は残り、テストは事後に叱るだけ。
定義を1つに寄せるという原則そのものを満たさない。却下。

**(b) pip-tools で `requirements.txt` を生成して COPY** — レイヤ分離は理想的。
しかし生成物をコミットするので、再生成忘れという形で二重管理が残る。却下。

**(c) uv を導入する** — build も lock も速い。しかし Dockerfile と
CI の6ジョブすべての install 方法を同時に変えることになり、
「Phase 1 を固定する」という今回の目的に対して差分が大きすぎる。Phase 2 で再評価。却下。

**(d) 依存を先に入れるレイヤを残すため pyproject だけ先に COPY し `pip install .`** —
setuptools バックエンドはソースが揃わないとビルドできないため、
**原理的に「依存だけ先に入れる」ができない**。この案は成立しない。

## Consequences

**良い側**
- 依存を足すとき触るのは `pyproject.toml` と `constraints.txt` の2つで、
  後者は「版」だけ。**パッケージ名を書く場所は1つ**になった
- 検査が入ったので、次に誰かが Dockerfile へ依存を書き足すと即座に落ちる
  （変異テストで発火を確認済み）
- `.dockerignore` によりビルドコンテキストが激減し、cpython-3.14 の `.pyc` が
  3.13 のイメージへ混入する経路も塞がった

**悪い側 / 引き受けた負債**
- **ソースを1文字変えると pip レイヤが再実行される。** BuildKit の cache mount
  （`--mount=type=cache,target=/root/.cache/pip`）でDLは回避されるが、
  再インストールの時間はかかる。ローカルでは数秒
- **CI では cache mount が `cache-from: type=gha` を跨いで保持されない。**
  CI の docker build は毎回フルDLになる。許容できなくなったら Phase 2 で
  multi-stage wheel build か uv を検討する
- `constraints.txt` の更新は手作業（コメントに手順を記載）。
  自動更新の仕組みは無く、**古くなっても検査は通る**。定期的な更新が要る
- `.dockerignore` で `tests` と `docs` を除いたため、
  イメージ内でテストを走らせることはできない
