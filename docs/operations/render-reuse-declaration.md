# ENFORCE を ON にする前に描画済みの動画（使用宣言の補完。ADR-0086 追補 R4）

`AVP_QUALITY_GATE_ENFORCE` を ON にすると、描画済みの動画は使用宣言を持たず gate が REVIEW にする。
render Activity が**同じ入力で skip する経路**で、入力 hash が「BGM・地図なし」を証明する動画にだけ not used を宣言する（INV-149）。
動画は描き直されない。補完を起こすには、ON にしたあと対象 Episode の pipeline を再実行して render Activity を通す。

## 影響を受ける Episode の数え方（読み取り専用。本番にはオーナーが実行する）

現行の final_video に BGM / MAP の宣言が揃っていない Episode:

```sql
SELECT a.episode_id, a.id AS artifact_id, a.version, a.created_at,
       count(d.id) AS declarations
FROM artifact_metadata a
LEFT JOIN quality_usage_declarations d
  ON d.episode_id = a.episode_id
 AND d.final_video_sha256 = a.sha256
 AND d.input_hash = a.input_hash
WHERE a.artifact_type = 'final_video'
  AND a.scene_id IS NULL
  AND a.superseded_at IS NULL
GROUP BY a.episode_id, a.id, a.version, a.created_at
HAVING count(d.id) < 2
ORDER BY a.created_at;
```

件数だけなら外側を `SELECT count(*) FROM (...) t` で包む。`declarations` が 0 は未宣言、1 は片方だけ。
BGM / 地図が ON になる前の本番では、ここに出る動画はすべて「BGM・地図なし」で描画されている（補完の対象）。
