# provider_health 契約テストの理由（ADR-0057）

`tests/unit/test_provider_health_contract.py`

- **fal の台帳 provider は全て同じアカウント族**: 残高は provider アカウント単位で尽きる。
  `fal_image` だけ止めて `fal_video` が submit を続けると、残高不足の拒否を毎シーン繰り返す。
  `ProviderCall` に fal の新しい値を足したとき表の更新漏れをここで落とす。
- **fal 以外は breaker の対象外**: 残高不足で Codex の台本生成や YouTube 投稿まで止めない
  （依頼の要件「別 provider を止めない」）。
- **鍵の形**: breaker の永続化キーはこの形で DB に入る。将来のチャンネル別アカウントは
  ラベルだけで表す。

## 結線（`tests/unit/test_gate_ports_wiring.py`）

- **本番の port が breaker / 保留の表に結線されている**: 日次ゲート（ADR-0058）は port が `None` だと
  「breaker closed・保留 0」として従来どおり動く設計。統合で結線を忘れると、残高不足でも日次が有料生成を
  始め、watchdog は保留中の Episode を stall と誤報する。どちらもテストでは緑のまま見逃せるので、
  結線点 `build_gate_ports` を直接検査する。
- **未解放保留の型は契約の `OpenHold` 1つ**: 並行開発中に実装側が同じ5フィールドの複製型を持っていた。
  片方だけ項目が増えると watchdog の分類が黙って壊れるので、複製が無いことを固定する（AGENTS.md §8）。

## 保留の寿命（`tests/unit/test_hold_lifecycle.py`, review-E P1-1）

- **保留状態を離れた Episode の行は数えない**: 解放は CLI だけなので、API 再開・完了・取消の後も行が残り、
  2 件で日次が HELD_BACKLOG のまま永久に止まり、watchdog の stall 検査も免除され続ける。読み取り側
  （`open_hold_count` / `list_open_holds` / `held_episode_ids_oldest_first`）が Episode の状態を見る。
  書き込み側の解放を足さないのは、解放は承認・予約照合・予算の検査と一体（INV-73）で、遷移フックから
  承認なしに行うと検査の迂回になるため。blocked / storyboard_ready は今も数える（反対側の検査）。
- **API 再開は開いた保留があれば 409**: 解放を CLI に一本化する。API が保留を無視して再開すると
  breaker・予算・未照合予約の検査を飛ばして有料呼び出しが再開しうる。CLI が解放後に API を呼ぶ経路
  （解放済みなら 202）と、保留状態を離れた古い行は拒否に使わないことも同じファイルで固定する。
