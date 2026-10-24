# provider_health 契約テストの理由（ADR-0057）

`tests/unit/test_provider_health_contract.py`

- **fal の台帳 provider は全て同じアカウント族**: 残高は provider アカウント単位で尽きる。
  `fal_image` だけ止めて `fal_video` が submit を続けると、残高不足の拒否を毎シーン繰り返す。
  `ProviderCall` に fal の新しい値を足したとき表の更新漏れをここで落とす。
- **fal 以外は breaker の対象外**: 残高不足で Codex の台本生成や YouTube 投稿まで止めない
  （依頼の要件「別 provider を止めない」）。
- **鍵の形**: breaker の永続化キーはこの形で DB に入る。将来のチャンネル別アカウントは
  ラベルだけで表す。
