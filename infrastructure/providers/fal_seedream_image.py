"""Seedream 4.5 text-to-image（fal queue）の ``ImageGenerator`` 実装（ADR-0017）。

- 9:16 だけを受け付け、provider へは 2160x3840 を要求する
  （1辺 1920..4096 の制約内で 9:16 ちょうど）。
  1080x1920 への縮小は worker 側の正規化が行う
- seed は送らない（再現性は input_hash による Artifact 再利用）
- 見積もり $0.04 / 枚（予約台帳の ``estimated_cost_usd``。確定額ではない）
"""

from __future__ import annotations

from decimal import Decimal
from typing import Any

from domain.errors import ProviderJobFailedError, ProviderRejectedError
from domain.production.ports import (
    ImageRequest,
    JobFailed,
    JobPending,
    JobStatus,
    JobSucceeded,
    MediaDestination,
    ProviderJobRef,
)
from infrastructure.providers.fal_queue import (
    DEFAULT_DOWNLOAD_MAX_BYTES,
    FalQueueClient,
    FalQueueState,
    FalSubmission,
)

SEEDREAM_ENDPOINT = "fal-ai/bytedance/seedream/v4.5/text-to-image"
SEEDREAM_WIDTH = 2160
SEEDREAM_HEIGHT = 3840
SEEDREAM_COST_USD = Decimal("0.04")
#: endpoint とパラメータの同一性。payload を変えたら版を上げる。
SEEDREAM_PROFILE_ID = f"fal-seedream-4.5:{SEEDREAM_WIDTH}x{SEEDREAM_HEIGHT}:safety-on:v1"


def build_seedream_payload(request: ImageRequest) -> dict[str, Any]:
    if request.aspect != "9:16":
        raise ProviderRejectedError(f"seedream adapter supports only 9:16, got {request.aspect}")
    return {
        "prompt": request.prompt,
        "image_size": {"width": SEEDREAM_WIDTH, "height": SEEDREAM_HEIGHT},
        "num_images": 1,
        "max_images": 1,
        "enable_safety_checker": True,
        "sync_mode": False,
    }


def _strip_urls(value: Any) -> Any:
    """evidence 用。取得 URL は保存しない（期限付きの所在であり、証拠は取得物そのもの）。"""
    if isinstance(value, dict):
        return {k: _strip_urls(v) for k, v in value.items() if k != "url"}
    if isinstance(value, list):
        return [_strip_urls(v) for v in value]
    return value


class FalSeedreamImageGenerator:
    generator_id = "fal-seedream"
    model_id = SEEDREAM_ENDPOINT
    generation_profile_id = SEEDREAM_PROFILE_ID

    def __init__(
        self, client: FalQueueClient, *, download_max_bytes: int = DEFAULT_DOWNLOAD_MAX_BYTES
    ) -> None:
        self._client = client
        self._download_max_bytes = download_max_bytes
        self._results: dict[str, dict[str, Any]] = {}

    def estimate_cost_usd(self, request: ImageRequest) -> float:
        return float(SEEDREAM_COST_USD)

    async def submit(self, request: ImageRequest) -> ProviderJobRef:
        submission = await self._client.submit(SEEDREAM_ENDPOINT, build_seedream_payload(request))
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
        if not _image_url(result):
            return JobFailed(message="fal seedream result has no image url")
        return JobSucceeded()

    async def download(self, ref: ProviderJobRef, dest: MediaDestination) -> None:
        submission = FalSubmission.from_ref(ref)
        url = _image_url(await self._result(ref, submission))
        if not url:
            raise ProviderJobFailedError("fal seedream result has no image url")
        await self._client.download(url, dest.write, max_bytes=self._download_max_bytes)

    async def describe_result(self, ref: ProviderJobRef) -> dict[str, Any]:
        """evidence として保存する結果 JSON（URL を除く）。"""
        result = await self._result(ref, FalSubmission.from_ref(ref))
        return {"request_id": FalSubmission.from_ref(ref).request_id, "result": _strip_urls(result)}

    async def _result(self, ref: str, submission: FalSubmission) -> dict[str, Any]:
        cached = self._results.get(ref)
        if cached is None:
            cached = await self._client.result(submission)
            self._results[ref] = cached
        return cached


def _image_url(result: dict[str, Any]) -> str | None:
    images = result.get("images")
    if isinstance(images, list) and images and isinstance(images[0], dict):
        url = images[0].get("url")
        return url if isinstance(url, str) and url else None
    return None


__all__ = [
    "SEEDREAM_COST_USD",
    "SEEDREAM_ENDPOINT",
    "SEEDREAM_PROFILE_ID",
    "FalSeedreamImageGenerator",
    "build_seedream_payload",
]
