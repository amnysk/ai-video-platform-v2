# deploy/backup — 定時実行の**例**（install しない）

ADR-0051 / `docs/operations/backup-restore.md`。ここのファイルは**例**で、repo は install・enable・
crontab 登録をしない（所有者が承認して自分で行う）。どれも `scripts/backup.py` を呼ぶだけで、
本番コンテナの再起動・再作成はしない。

| ファイル | 内容 |
|---|---|
| `avp-backup.env.example` | 保存先と任意の通知・offsite。**既定は全て空**（空なら実行を拒否 / 通知しない / 送信しない） |
| `avp-backup.service` / `.timer` | 毎日 backup（`run`）。user unit 想定（rootless docker の所有者で実行） |
| `avp-backup-drill.service` / `.timer` | 週 1 回、最新の backup を隔離先で復旧演習 → 検証済みにする。成功後に prune |
| `crontab.example` | systemd を使わない場合の同等の例 |
