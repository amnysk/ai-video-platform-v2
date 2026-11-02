# BGM 自動選曲・DB カタログ・フォールバックのテストの設計理由（ADR-0087 §4〜§7）

各テストが「何の事故を防ぐか」を残す。落ちたら実装を直す（AGENTS.md §2）。

| テスト | 防ぐ事故 |
|---|---|
| `tests/unit/test_bgm_auto_select.py` | 同じ台本が実行のたびに違う曲になる / 隔離・失効・版違いの自動承認・ブランド不許可の曲が選ばれる / 点の根拠が説明できない |
| `tests/unit/test_bgm_mix_offset.py` | 所有者が試聴で承認した CK61 の +6 dB が再現されない（`shift_levels(+6)` と同一の `BgmMixPlan`）/ 持ち上げで上限 -12 dB を超える |
| `tests/unit/test_bgm_catalog_repository.py` | 同時実行で選曲が上書きされる（INV-153）/ 同じ内容の曲が二重登録される / 隔離を飛ばした承認 |
| `tests/contract/test_migration_0021_bgm.py` | 旧コードを壊す migration（expand-only でない・FK・走査器に掛かる）/ `BgmTrack` の列の取りこぼし |
| `tests/unit/test_render_activities_bgm_auto.py` | 梯子の無限ループ・3 曲目への逸脱 / 改竄・欠落した原本の混合 / 再試行で選曲が変わり input_hash がずれる / BGM 無しの動画に BGM の識別子や証跡が残る / fallback 経路が fal や production を動かす |
| `tests/unit/test_bgm_catalog_import.py` | 取込が承認を持ち越す / dry-run で書く / `--yes` 無しで DB に書く |

音声の実測（ffmpeg）は fake の `SpeechMarginProbe` に差し替えている。実測の正しさは既存の
`test_bgm_real_ffmpeg_margin.py` が持つ。本物の PostgreSQL での JSONB・同時 INSERT は integration で確認する（未実施）。
