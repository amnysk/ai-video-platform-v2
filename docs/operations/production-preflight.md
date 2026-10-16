# fal preflight（ADR-0030）の運用

```bash
python scripts/production-preflight.py
```

worker（`workers/production_video/run_worker.py`）と同じ `Settings` 解決・同じ
`FalStorageClient` 構築を使う（`infrastructure/production/preflight.py`）。各行は
`[OK|FAIL|UNVERIFIED][local|network]` を先頭に出す。`local` はプロセス外に一切出ない
チェック、`network` は実際に fal へ到達するチェック。終了コードは検証済み（OK/FAIL）だけで
決まる。`UNVERIFIED` は失敗として数えない ── かつ healthy としても数えない。

## `/storage/auth/token` の課金有無の調査記録（2026-09-23）

fal 公式ソースを次のとおり確認した結果、**このエンドポイントが非課金であることを断定できる
記述は見つからなかった**（同様に、課金される記述も見つからなかった）。

| 調査先 | 見つかったこと |
|---|---|
| `fal.ai/docs/documentation/model-apis/pricing` | 課金の説明は「生成した出力に対して課金する」
  「成功した出力にのみ課金する」「サーバーエラーやキュー待ち時間には課金しない」と、**Model API
  の推論結果に対する課金だけ**を明示的に述べている。ストレージ/CDN/トークン交換には一切言及が無い |
| `fal.ai/docs/documentation/development/working-with-files` | アップロード方法（クライアント
  API・REST・CLI）の説明のみ。課金・コストへの言及なし |
| `fal.ai/docs/documentation/development/file-storage` | 同上。課金・コストへの言及なし |
| `x-fal-billable-units` ヘッダ（fal-client SDK・fal Python SDK を検索） | これは **Model API
  の推論結果**にサーバー側が付与し、カスタムモデルのホスティング側が課金単位を報告するための
  ヘッダであり、ストレージ/CDN 呼び出しとは無関係と判明した。以前の調査メモにあった
  「課金は `x-fal-billable-units` で opt-in」という示唆は、調べ直した結果ストレージ課金とは
  無関係な別の仕組み（推論結果の課金報告）を指しており、非課金性の根拠にはならない |

**結論**: 公式情報だけでは非課金性を断定できない。`AVP_PREFLIGHT_CONFIRMED_NONBILLING=1` を
自動設定しない・既定を変えない（AGENTS.md §9、ADR-0030 §Decision(3)）。運用者が fal に直接
確認できた場合のみ、上記の env var を明示的に設定して実呼び出し診断を有効にすること。

### 再調査が必要になる条件

- fal が pricing / file-storage ドキュメントを更新し、ストレージ操作の課金有無を明記した場合
- `fal-client` SDK が `/storage/auth/token` のレスポンスに課金を示す新しいフィールド・
  ヘッダーを追加した場合
