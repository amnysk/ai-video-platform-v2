# BGM 自動化の導入手順（deploy 準備と flag を ON にする順序）

Status: **Proposed**（ADR-0087 §9 / §10、INV-158 / INV-159）。**この手順は所有者が承認するまで実行しない。**
`[R]` は読み取り専用（承認前に実行してよい）、`[W]` は本番の状態を変える（所有者の明示的な承認が要る。AGENTS.md §9）。
全ての flag は現在 OFF で、導入前と同一の挙動。この手順書はコードを deploy しただけでは何も変わらないことを前提にする。

## 承認が要る操作の一覧

次は**全て所有者の承認**を取ってから行う。エージェントが「条件付きの指示」で代行してよいのは release preflight の
委任承認までで、flag を ON にする release は委任では BLOCKED になる（`docs/operations/release-runbook.md` §1.5）。

| 操作 | 承認が要る理由 |
|---|---|
| deploy（`make deploy-workers`） | 本番のコンテナを作り直す。migration 0021 を適用する |
| いずれかの flag を ON にする | 動画の中身・投稿の判断が変わる |
| 本番 DB の変更（migration 0021、カタログの取込、承認の記録） | 戻しにくい |
| 保留中の Episode の解放（`scripts/quality-approve.py --execute`） | REVIEW を人が解放する唯一の経路（INV-129） |
| fal の呼び出し（BGM 経路は呼ばない。Episode の再生成を伴う操作） | 課金 |
| YouTube への投稿（private のみ。公開範囲は自動で変えない） | 外部への副作用 |
| 収集の Temporal Schedule を作る | 外部ネットワーク（Commons）への定期アクセスが始まる |

## 有効化の順序

`AVP_UPLOAD_DESCRIPTION_COMPOSED`（COMPOSED）→ `AVP_QUALITY_GATE_ENFORCE`（ENFORCE）→ `BGM_ENABLED`（BGM）→
`BGM_ACQUISITION_ENABLED`（ACQUISITION）。前の段が ON でないと次の worker は**起動時に止まる**（INV-159）。
`GEO_MAPS_ENABLED` は OFF のまま触らない。各段は**別々の変更として**行い、段ごとに下の確認を済ませてから次へ進む。
値は対象の `.env` に 1 行書き、該当 service を再作成する（`docker compose` の環境変数はコンテナ作成時に決まる）。

### 0. deploy（flag は全て OFF のまま）`[W]`

1. `[R]` 対象 SHA を確定し、`docs/operations/release-runbook.md` §1 の preflight（承認の `enables` は `[]`）を通す。
2. `[R]` migration 0021 が expand-only であること（新しい 3 表だけを足し、既存表を変えない。ADR-0087 §4）を確認する。
   `alembic heads` が 1 つであること（0020 が他の枝から入った場合、後に入る側が `down_revision` を付け替える）。
3. `[W]` `make deploy-workers`。migrate が 0021 を適用する。収集 worker は `--profile core` に含まれないので**起動しない**。
4. `[R]` `python scripts/release-preflight.py --verify-post-deploy ...`（runbook §3）。READY 以外は先へ進まない。
5. `[R]` 確認: 全 flag OFF の間の挙動が導入前と同一であること。直近の Episode が従来どおり進む、
   `alembic current` が 0021、`bgm_tracks` / `bgm_selections` / `bgm_acquisition_runs` が空。

ロールバック: コードだけを戻す（runbook §4。`alembic downgrade` には頼らない。新しい 3 表は FK を張らないので、残しても旧コードに影響しない）。

### 1. COMPOSED（説明欄の構成）`[W]`

`.env`: `AVP_UPLOAD_DESCRIPTION_COMPOSED=true` → api / pipeline / render / upload を再作成。

- 確認 `[R]`: 次の投稿の説明欄に出典・クレジット欄が組まれる。`quality.decision` のログが出る。
- ロールバック: 値を `false`（または行を削除）にして再作成。

### 2. ENFORCE（品質ゲートの強制）`[W]`

`.env`: `AVP_QUALITY_GATE_ENFORCE=true` → api / pipeline / render / production / **upload** を再作成。
upload worker はこの値で、投稿 Activity の中に最後の門（INV-158）を持つ。

- 事前に決める（R4）: 描画済みで使用宣言の無い動画（導入前の動画）は REVIEW になる。保留のまま持つか、
  所有者が個別に解放するか。**一括解放はしない**。
- 確認 `[R]`: 宣言のある Episode は PASS で投稿される。宣言の無い Episode は `upload_gate` が
  `quality: report_needs_review` で保留する。保留率は `scripts/quality_rates.py`（読み取り専用。ADR-0087 §11。別の枝で実装中で、マージ前は無い）で見る。
  直接 `UploadWorkflow` を起動しても PASS / WARN 以外は YouTube に届かない（`UploadQualityRefusedError` で needs_input）。
- 保留した Episode の解放 `[W]`: API 認証が無効の間は host で所有者が
  `python scripts/quality-approve.py <episode_id> --owner-approval OWNER-<yyyymmdd>-<id>`（dry-run）で理由を読み、
  `--execute` を付けて解放する。BLOCK は承認で解放できない。
- ロールバック: 値を `false` にして再作成。**BGM が ON の間は戻せない**（BGM の worker が起動しない）。先に BGM を OFF にする。

### 3. BGM（描画への BGM の組み込み）`[W]`

前提: COMPOSED と ENFORCE が ON、かつ使う曲が DB に**所有者承認済みで**入っていること。

1. カタログの取込 `[W]`（本番 DB の変更）。所有者が試聴・ライセンス確認を済ませた曲だけを入れる。
   CK61（`bgm-cc0-ambient-ck61`、Commons `File:Ambient_music_test,_Yamaha_CK61.flac`、CC0 1.0）は、実測で声より約 20 LU 低く
   スマホではほぼ聞こえないため、**取込時に `--mix-gain-offset-db 6`** を付ける（+6 dB でも発話との差 12 LU の余裕を満たすことは
   確認済み。Shika は +3 dB で既に 12 LU を割るので持ち上げない）。
   ```bash
   python scripts/bgm_catalog_import.py --catalog <catalog dir> --track bgm-cc0-ambient-ck61 --mix-gain-offset-db 6
   ```
   取込は承認を持ち越さない。取込後に所有者が曲ごとの承認を記録する（`license_approver_kind=owner`）。
   `scripts/bgm_catalog_import.py` は ADR-0087 §4 で計画された取込スクリプト（別の枝で実装中。マージ前は無い）。
   引数は実装に従う（`--help`）。**所有者の承認の前に実行しない**。
2. `.env`: `BGM_ENABLED=true` → render / upload（と、使うなら収集 worker）を再作成。
   COMPOSED / ENFORCE が OFF なら、render と upload は `SystemExit` で起動しない。
- 確認 `[R]`: 直近の Episode を BGM 付きで描画した動画の `bgm.selection` ログ（曲・適用 gain・理由）、gate の BGM 検査が PASS、
  発話中の声と BGM の差が 12 LU 以上。fal の予約台帳が増えていない（BGM は fal を呼ばない。INV-137）。
  試聴: `docs/operations/bgm-preview.md`。
- ロールバック: 値を `false` にして render / upload を再作成。投稿済みの動画は変更しない。

### 4. ACQUISITION（Commons からの自動収集）`[W]`

前提: BGM が ON。収集 worker は**既定で起動しない**（compose の profile `bgm-acquisition`）。

1. `.env`: `BGM_ACQUISITION_ENABLED=true`。
2. worker を起動 `[W]`: `docker compose --profile bgm-acquisition up -d --no-deps bgm-acquisition-worker`
   （`deploy-workers.sh` の対象ではない。名前を挙げて up すると profile が有効になるため、deploy の一覧に入れていない）。
   BGM が OFF なら `SystemExit` で起動しない。コンテナは `avp2-worker` と同じイメージなので、deploy で SHA が変わったら
   このコマンドで作り直す（`scripts/workers-versions.sh` で版を見る）。
3. 収集の **Temporal Schedule を作るのは所有者だけ**（この compose も deploy も作らない）。Schedule を作るまで収集は
   走らない。作る前に、Wikimedia Commons へのアクセス頻度・検索語・自動承認の規則（CC0・本人の作品・制限なし・係争なしだけ。
   それ以外は隔離 = REVIEW）を所有者が確認する。
- 確認 `[R]`: `bgm_acquisition_runs` に実行ごとの監査が残る。自動承認は CC0 だけで、それ以外は隔離に入る。ネットワークは
  許可されたホスト（Commons）だけ。
- ロールバック: Schedule を一時停止/削除（所有者）→ `docker compose --profile bgm-acquisition stop bgm-acquisition-worker`
  → `BGM_ACQUISITION_ENABLED=false`。取り込み済みの曲は残る（使うかは BGM とカタログの承認で決まる）。

## flag ごとのロールバックの早見表

| 戻す flag | 先に戻すもの | 戻し方 | 戻した後 |
|---|---|---|---|
| ACQUISITION | なし | Schedule 停止 → worker 停止 → `false` | 収集は止まる。曲は残る |
| BGM | ACQUISITION | `false` → render / upload 再作成 | 描画は BGM 無し（導入前と同一） |
| ENFORCE | BGM, ACQUISITION | `false` → 再作成 | 自動・手動の gate と投稿 Activity の門が OFF（導入前と同一） |
| COMPOSED | ENFORCE, BGM | `false` → 再作成 | 説明欄は従来の組み立て |

## 全体を止めたいとき

投稿を即座に止めるには `UPLOADS_PAUSED=true`（要再起動）か operational switch `uploads_paused`
（`scripts/operational-switch.py`。再起動不要）。`[W]` だが「止める」方向なので所有者へ事後報告でよいかは所有者が決める。
