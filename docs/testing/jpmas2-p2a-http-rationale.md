# JP-MAS-002 P2-A（HTTP adapter・観測・CORS）のテストの理由（ADR-0044 §4/§5）

テストを足した理由を、足したのと同じコミットで残す。service 本体（`application/service.py`）のテストは
別の変更が持つ。ここは「HTTP が catalog と一致し、service を呼ぶだけであること」を固定する。

## 3. 観測（tests/unit/test_application_http_observer.py）

- 1 操作 = `application.operation.completed` 1 行、ADR §5 の項目が入る。`succeeded`/`rejected`/`failed` の写像と
  level（failed だけ ERROR）。未取得の値は付けない。ログの故障は呼び出し元へ伝えない（INV-38）。

## 4. CORS（tests/unit/test_application_http_cors.py）

- 既定（空・空白だけ）は middleware を付けない。設定した origin だけ、GET/POST と 3 つの header（Authorization・
  Content-Type・X-Correlation-ID）を許し、credentials は許さない。`*` は起動時に拒否（token は header で渡すので
  全許可にする理由が無い）。

## 設定キー `API_CORS_ORIGINS`（AGENTS.md §8 の grep）

`grep -rn "api_cors_origins\|API_CORS_ORIGINS" apps/ workers/ domain/ infrastructure/ contracts/ docs/`（追加前）:
ヒット 1 件（`docs/decisions/0044-…md`: 設計書。読み手／書き手のコードは 0 件、無関係 0 件）。
追加後の読み手は `apps/api/main.py:create_app`（`infrastructure.config.cors_origins` 経由）の 1 か所、書き手は環境変数のみ。
