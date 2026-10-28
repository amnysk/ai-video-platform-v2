"""帰属表記（INV-98）。指定文は**全文を置くか、置けないなら止める**。切り詰めない。"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass

from contracts.music import BgmTrack, MusicCueSheet, is_blank_text
from contracts.upload import sanitize_youtube_text

#: クレジット行どうしの区切り（説明欄に置く形。バイト数の計算に含める）。
CREDIT_SEPARATOR = "\n"


class CreditsError(ValueError):
    """cue sheet とカタログが食い違う。帰属表記を推測で作らず、呼び出し側が止める。"""


def credits_for(cue_sheet: MusicCueSheet, catalog: Iterable[BgmTrack]) -> tuple[str, ...]:
    """cue sheet が使う曲のうち帰属必須のものの指定文（カタログの原文）を初出順・重複なしで返す。"""
    by_id = {t.track_id: t for t in catalog}
    out: list[str] = []
    for cue in cue_sheet.cues:
        track = by_id.get(cue.track_id)
        if track is None:
            raise CreditsError(f"cue for unknown track {cue.track_id!r}")
        if cue.source_sha256 != track.file_sha256:
            raise CreditsError(f"cue source_sha256 does not match catalog for {cue.track_id!r}")
        if track.attribution_required:
            text = track.exact_attribution_text
            if not text or is_blank_text(text):
                raise CreditsError(f"{cue.track_id!r} requires attribution but has no text")
            if sanitize_youtube_text(text) != text:
                raise CreditsError(f"{cue.track_id!r} text would be rewritten when sent")
            if cue.credits_text != text:
                raise CreditsError(
                    f"cue credits_text differs from the catalog for {cue.track_id!r}"
                )
            if text not in out:
                out.append(text)
        elif cue.credits_text is not None:
            raise CreditsError(f"cue carries credits_text for {cue.track_id!r}, which needs none")
    return tuple(out)


@dataclass(frozen=True, slots=True)
class CreditsFit:
    """``ok=False`` は BLOCK。``credits`` は常に**元の全文**で、切り詰めた版は存在しない。"""

    ok: bool
    credits: tuple[str, ...]
    #: 説明欄へ実際に送る文字列（sanitize 後）。``required_bytes`` はこれの UTF-8 バイト数。
    rendered: str
    required_bytes: int
    budget_bytes: int


def fit_credits(description_budget_bytes: int, credits: tuple[str, ...]) -> CreditsFit:
    # 送信時と同じ sanitize（contracts.upload の唯一の定義）の後で測る。生のバイト数では足りない。
    rendered = sanitize_youtube_text(CREDIT_SEPARATOR.join(credits))
    required = len(rendered.encode("utf-8"))
    ok = required == 0 or required <= description_budget_bytes
    return CreditsFit(
        ok=ok,
        credits=credits,
        rendered=rendered,
        required_bytes=required,
        budget_bytes=description_budget_bytes,
    )
