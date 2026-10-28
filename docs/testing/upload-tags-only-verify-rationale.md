# テスト設計の根拠: タグだけの照合の確認ツール（ADR-0075、INV-110）

守るもの: 本番で marker なし description に切り替える前に、「タグだけの照合が description の照合と同じ動画を見つける」
ことを所有者が実データで確かめられること、そのツールが書き込みを一切しないこと、flag が勝手に ON にならないこと。
実 YouTube には出ない（fixture と `httpx.MockTransport`）。実機の確認は所有者の手作業（実 API をエージェントは呼ばない）。

## `tests/unit/test_verify_tags_only_match.py`

| テスト | 水準 | なぜ必要か / 落ちたら何が起きているか |
|---|---|---|
| `test_video_with_marker_in_both_agrees` | 純粋関数 | 両方にマーカーがある動画は一致。基準の正常系 |
| `test_legacy_description_only_video_is_a_mismatch` | 純粋関数 | 旧方式の動画はタグだけでは見つからない。これを「一致」と数えると、ON にした後に過去動画を見落とす（R2 の核心） |
| `test_tag_on_a_different_video_is_a_mismatch` | 純粋関数 | 同じマーカーが別の動画のタグにあると、別の動画を採用する。「同じ動画か」を video id で比べている確認 |
| `test_tags_only_new_style_videos_are_counted_but_not_compared` | 純粋関数 | 新方式の動画は比較の分母に入れない（入れると一致率が水増しされる） |
| `test_non_private_and_markerless_videos_are_not_compared` | 純粋関数 | private 以外・マーカー無し・取得できなかった動画を分母から外す |
| `test_nothing_to_compare_is_inconclusive_not_agreement` | 純粋関数 | 比較対象 0 件を「100% 一致」にしない（空集合で自明に通る） |
| `test_a_truncated_listing_is_never_a_conclusion` | 純粋関数 | 一覧を読み切れていないとき、一致していても結論にしない |
| `test_partial_line_match_in_description_is_not_a_marker` | 純粋関数 | 行の一部の一致を数えない（adapter と同じ定義） |
| `test_cli_fixture_*`（2件）| CLI | 終了コード（一致 0 / 不一致 1）と、出力に title・description・マーカーが出ないこと（件数と video id だけ）。fixture には sentinel 文字列を入れて漏れを検出する |
| `test_cli_requires_an_explicit_mode` / `test_cli_live_without_credentials_*` | CLI | 引数なしで実 API に行かない。認証情報が無ければ何も呼ばない |
| `test_live_readonly_token_reads_with_get_only_and_reports` | live 経路 | YouTube への要求がすべて GET で、3種類の読み取りだけ。POST は OAuth ホストの token 更新・scope 確認だけ |
| `test_live_refuses_any_token_that_is_not_readonly_only_before_calling_youtube`（6 scope） | live 経路 | upload / 全権 / force-ssl / analytics / scope 無しのどれでも、YouTube を1回も呼ばずに拒否する。読み取り専用の保証の本体 |
| `test_live_page_limit_is_reported_as_truncated_not_agreement` | live 経路 | ページ上限で打ち切ったら結論にしない |
| `test_verification_code_has_no_write_call_and_never_imports_the_upload_adapter`（script / module） | 静的（AST） | 投稿 adapter（insert を持つ）を import しない、insert / update / delete / put / patch の呼び出しが無い、POST は scope 確認の1関数だけ。後から書き込みを足す変更を止める |
| `test_verification_code_names_no_write_scope` | 静的 | 書き込み系 scope の名前をコードに書かない |
| `test_the_description_without_marker_flag_defaults_off` | 契約 | 既定 OFF の固定（既存 `test_upload_metadata_lint.py` の既定の検査と同じ値を、確認ツールの前提として別に固定） |
| `test_no_deploy_artifact_turns_the_description_without_marker_flag_on` | 静的 | compose / deploy / Dockerfile / Makefile / `.env*` に flag を書いて本番で ON にする変更を止める |

`contracts/upload.py` の `marker_in_tags` / `marker_in_description_lines` は、adapter の照合の内部にあった判定を取り出した
だけ（挙動は同じ。既存の `test_youtube_uploader.py` / `test_upload_metadata_lint.py` が検査する）。
