"""画像を 9:16（1080x1920）へ揃える（ADR-0017）。計画はドメインの純粋関数が決める。"""

from __future__ import annotations

import io
from dataclasses import dataclass

from PIL import Image, UnidentifiedImageError

from domain.errors import MediaValidationError
from domain.production.media import NormalizationRejected, plan_9x16_normalization


@dataclass(frozen=True, slots=True)
class NormalizedImage:
    data: bytes
    mime: str
    width: int
    height: int


def normalize_image_9x16(data: bytes) -> NormalizedImage:
    """中央切り出し + LANCZOS 縮拡 → PNG。同じ入力からは同じバイト列になる。"""
    try:
        with Image.open(io.BytesIO(data)) as source:
            source.load()
            plan = plan_9x16_normalization(*source.size)
            if isinstance(plan, NormalizationRejected):
                raise MediaValidationError(f"cannot normalize image: {plan.reason}")
            image = source.convert("RGB").crop(plan.crop_box)
            if image.size != (plan.target_width, plan.target_height):
                image = image.resize(
                    (plan.target_width, plan.target_height), Image.Resampling.LANCZOS
                )
            out = io.BytesIO()
            image.save(out, format="PNG", optimize=False)
    except MediaValidationError:
        raise
    except (UnidentifiedImageError, OSError, ValueError) as exc:
        raise MediaValidationError(f"image is not decodable: {exc}") from exc
    return NormalizedImage(
        data=out.getvalue(), mime="image/png", width=plan.target_width, height=plan.target_height
    )


__all__ = ["NormalizedImage", "normalize_image_9x16"]
