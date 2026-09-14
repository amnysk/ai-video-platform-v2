"""Seedance 2.0 fast image-to-video（fal queue）の ``VideoGenerator`` 実装（ADR-0017 Phase 4C）。

- 元画像は ``prepare`` で fal CDN へ上げる（非課金）。``PaidJobRunner`` は ``prepare`` を
  **予約の前**に呼ぶので、アップロード失敗は台帳に何も残さない。``submit`` は準備済みの
  ``FalPreparedVideoRequest`` しか受け付けない（dispatch 後に I/O で失敗しないため）
- 尺は文字列 ``"4"``..``"15"``（整数秒）。``"auto"`` は送らない
- 9:16 / 720p / 音声なし。seed / camera_fixed は送らない（API に無い）
- 見積もり $0.2419 / 秒（720p）
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from decimal import Decimal
from typing import Any

from domain.artifact.hashing import sha256_hex
from domain.errors import ProviderJobFailedError, ProviderRejectedError
from domain.production.ports import (
    JobFailed,
    JobPending,
    JobStatus,
    JobSucceeded,
    MediaDestination,
    ProviderJobRef,
    VideoRequest,
)
from infrastructure.providers.fal_queue import (
    DEFAULT_DOWNLOAD_MAX_BYTES,
    FalQueueClient,
    FalQueueState,
    FalSubmission,
)
from infrastructure.providers.fal_storage import FalStorageClient

SEEDANCE_ENDPOINT = "bytedance/seedance-2.0/fast/image-to-video"
SEEDANCE_MIN_SECONDS = 4
SEEDANCE_MAX_SECONDS = 15
SEEDANCE_COST_PER_SECOND_USD = Decimal("0.2419")
#: endpoint とパラメータの同一性。payload を変えたら版を上げる。
SEEDANCE_PROFILE_ID = "fal-seedance-2.0-fast:720p:9x16:noaudio:v1"
_IMAGE_EXT = {"image/png": "png", "image/jpeg": "jpg", "image/webp": "webp"}


@dataclass(frozen=True, slots=True)
class FalPreparedVideoRequest(VideoRequest):
    """元画像のアップロードを済ませた request。``image_url`` は fal CDN の https URL。"""

    image_url: str


def seedance_seconds(duration_ms: int) -> int:
    """整数秒へ切り上げ、4..15 に収める。"""
    seconds = math.ceil(max(duration_ms, 0) / 1000)
    return min(SEEDANCE_MAX_SECONDS, max(SEEDANCE_MIN_SECONDS, seconds))


def build_seedance_payload(request: FalPreparedVideoRequest) -> dict[str, Any]:
    if request.aspect != "9:16":
        raise ProviderRejectedError(f"seedance adapter supports only 9:16, got {request.aspect}")
    seconds, remainder = divmod(request.duration_ms, 1000)
    if remainder or not SEEDANCE_MIN_SECONDS <= seconds <= SEEDANCE_MAX_SECONDS:
        raise ProviderRejectedError(
            f"seedance duration must be whole seconds in 4..15, got {request.duration_ms} ms"
        )
    return {
        "prompt": request.prompt,
        "image_url": request.image_url,
        "duration": str(seconds),
        "aspect_ratio": "9:16",
        "resolution": "720p",
        "generate_audio": False,
    }


def _strip_urls(value: Any) -> Any:
    if isinstance(value, dict):
        return {k: _strip_urls(v) for k, v in value.items() if k != "url"}
    if isinstance(value, list):
        return [_strip_urls(v) for v in value]
    return value


class FalSeedanceVideoGenerator:
    generator_id = "fal-seedance"
    model_id = SEEDANCE_ENDPOINT
    generation_profile_id = SEEDANCE_PROFILE_ID

    def __init__(
        self,
        client: FalQueueClient,
        storage: FalStorageClient,
        *,
        download_max_bytes: int = DEFAULT_DOWNLOAD_MAX_BYTES,
    ) -> None:
        self._client = client
        self._storage = storage
        self._download_max_bytes = download_max_bytes
        self._results: dict[str, dict[str, Any]] = {}

    def supported_duration_ms(self, requested_ms: int) -> int:
        return seedance_seconds(requested_ms) * 1000

    def estimate_cost_usd(self, request: VideoRequest) -> float:
        seconds = Decimal(request.duration_ms) / 1000
        return float(seconds * SEEDANCE_COST_PER_SECOND_USD)

    async def prepare(self, request: VideoRequest) -> FalPreparedVideoRequest:
        """元画像を fal CDN へ上げる（非課金・予約前）。"""
        if isinstance(request, FalPreparedVideoRequest):
            return request
        ext = _IMAGE_EXT.get(request.source_image_mime)
        if ext is None:
            raise ProviderRejectedError(
                f"seedance source image mime {request.source_image_mime!r} not supported"
            )
        url = await self._storage.upload(
            request.source_image,
            request.source_image_mime,
            f"{sha256_hex(request.source_image)[:16]}.{ext}",
        )
        return FalPreparedVideoRequest(
            prompt=request.prompt,
            source_image=request.source_image,
            source_image_mime=request.source_image_mime,
            duration_ms=request.duration_ms,
            aspect=request.aspect,
            image_url=url,
        )

    async def submit(self, request: VideoRequest) -> ProviderJobRef:
        if not isinstance(request, FalPreparedVideoRequest):
            # prepare を通さない呼び出しは設計違反。HTTP を送る前に止める
            raise ProviderRejectedError("seedance submit requires a prepared request")
        submission = await self._client.submit(SEEDANCE_ENDPOINT, build_seedance_payload(request))
        return submission.to_ref()

    async def poll(self, ref: ProviderJobRef) -> JobStatus:
        submission = FalSubmission.from_ref(ref)
        status = await self._client.status(submission)
        if status.state is FalQueueState.PENDING:
            return JobPending()
        try:
            result = await self._result(ref, submission)
        except ProviderRejectedError as exc:
            return JobFailed(message=str(exc), rejected=True)
        except ProviderJobFailedError as exc:
            return JobFailed(message=str(exc))
        if not _video_url(result):
            return JobFailed(message="fal seedance result has no video url")
        return JobSucceeded()

    async def download(self, ref: ProviderJobRef, dest: MediaDestination) -> None:
        url = _video_url(await self._result(ref, FalSubmission.from_ref(ref)))
        if not url:
            raise ProviderJobFailedError("fal seedance result has no video url")
        await self._client.download(url, dest.write, max_bytes=self._download_max_bytes)

    async def describe_result(self, ref: ProviderJobRef) -> dict[str, Any]:
        submission = FalSubmission.from_ref(ref)
        result = await self._result(ref, submission)
        return {"request_id": submission.request_id, "result": _strip_urls(result)}

    async def _result(self, ref: str, submission: FalSubmission) -> dict[str, Any]:
        cached = self._results.get(ref)
        if cached is None:
            cached = await self._client.result(submission)
            self._results[ref] = cached
        return cached


def _video_url(result: dict[str, Any]) -> str | None:
    video = result.get("video")
    if isinstance(video, dict):
        url = video.get("url")
        return url if isinstance(url, str) and url else None
    return None


__all__ = [
    "SEEDANCE_COST_PER_SECOND_USD",
    "SEEDANCE_ENDPOINT",
    "SEEDANCE_PROFILE_ID",
    "FalPreparedVideoRequest",
    "FalSeedanceVideoGenerator",
    "build_seedance_payload",
    "seedance_seconds",
]
