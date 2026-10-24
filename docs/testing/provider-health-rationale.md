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
