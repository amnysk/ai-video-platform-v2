# JP-MAS-002 P2-A（HTTP adapter・観測・CORS）のテストの理由（ADR-0044 §4/§5）

テストを足した理由を、足したのと同じコミットで残す。service 本体（`application/service.py`）のテストは
別の変更が持つ。ここは「HTTP が catalog と一致し、service を呼ぶだけであること」を固定する。

## 3. 観測（tests/unit/test_application_http_observer.py）

- 1 操作 = `application.operation.completed` 1 行、ADR §5 の項目が入る。`succeeded`/`rejected`/`failed` の写像と
  level（failed だけ ERROR）。未取得の値は付けない。ログの故障は呼び出し元へ伝えない（INV-38）。

