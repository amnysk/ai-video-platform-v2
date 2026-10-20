# 日次異常レポートのテスト根拠

- `tests/unit/test_anomaly_report_domain.py`
  - 同じ停滞 Episode の毎日の再検出を1 incident に畳む: 「累積250/227」を失敗数と読んだ誤認の再発防止。
  - 窓ごとの rows / incidents / new_incidents: 古い incident の再検出を「新しい失敗」と数えない。
  - 再発中の判定は「未解決 かつ 直近24hに検出」: 自然に消えた古い未解決を再発扱いしない。解決は最新行で判断。
  - 共通原因は「複数 incident が1種類の原因を共有する時」だけ立てる。
  - preflight 入口はカテゴリ全部を返す（欠けた名前を 0 と誤読しない）。
- `tests/unit/test_anomaly_events_loader.py`
  - NOT_COMPLETED の原因を同じ Episode の停滞理由に寄せる（detail に原因が無い実データの形）。
  - 完成済み Episode の provider 失敗は解決済み。fal 以外の script/storyboard の失敗は数えない（二重計上防止）。
  - 欠けた日の判定は最初の slot 以降だけ、6:30 より前は今日を欠けにしない（誤警報防止）。
  - Temporal リーダーは GET だけ、子 workflow の失敗イベントを親の失敗と取り違えない（本番で実際に踏んだ）、
    到達不能でも例外にせず error を返す。
  - 実 PostgreSQL では UUID 型、sqlite では文字列になる episode_id を同じ文字列に揃える。
- `tests/architecture/test_anomaly_report_read_only.py`: 書き込み動詞・変更系呼び出しが無いこと、domain が純粋なこと。
- `tests/contract/test_logging_query_definitions.py`: 定義が未適用・無効・通知先なしで、読み取り DSL のみ、
  使うイベント名が emission-points に実在すること。
