"""render が書く使用宣言（ADR-0081 §3 / ADR-0086 §2）。非致命: 書けなくても描画は成功扱い。

宣言が無い完成動画は gate が REVIEW にする（USAGE_DECLARATION_MISSING。PASS に倒れない）ので、
ここで失敗を握りつぶしても安全側に倒れる。ログには ID と例外の型だけを出す。
"""

from __future__ import annotations

import logging
from datetime import datetime

from contracts.quality_records import UsageFeature
from domain.quality.records import CheckSubject, QualityRecordWriter

logger = logging.getLogger(__name__)

DECLARED_BY = "render"


async def declare_render_usage(
    writer: QualityRecordWriter,
    *,
    episode_id: str,
    final_video_sha256: str,
    input_hash: str,
    bgm_evidence_ref: str | None,
    map_evidence_ref: str | None,
    declared_at: datetime,
) -> bool:
    """BGM・地図の使用宣言を書く。``*_evidence_ref`` が ``None`` なら「使っていない」。

    全部書けたら True。どれか 1 つでも書けなければ False（例外は外へ出さない）。
    """
    try:
        subject = CheckSubject(episode_id, final_video_sha256, input_hash)
    except ValueError:
        logger.warning("quality usage not declared episode=%s reason=invalid_subject", episode_id)
        return False
    ok = True
    for feature, ref in (
        (UsageFeature.BGM, bgm_evidence_ref),
        (UsageFeature.MAP, map_evidence_ref),
    ):
        try:
            await writer.declare_usage(
                subject,
                feature,
                used=ref is not None,
                declared_by=DECLARED_BY,
                declared_at=declared_at,
                evidence_ref=ref,
            )
        except Exception as exc:  # noqa: BLE001 - 非致命。宣言が無ければ gate は REVIEW
            ok = False
            logger.warning(
                "quality usage not declared episode=%s feature=%s error=%s",
                episode_id,
                feature.value,
                type(exc).__name__,
            )
    return ok
