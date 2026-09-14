"""Seedance adapter の payload・尺の丸め・見積もり・結果の写像、動画 prompt builder（Phase 4C）。"""

from __future__ import annotations

import json

import httpx
import pytest

from contracts.artifacts import StoryboardScene, StoryboardVisualKind
from domain.errors import ProviderRejectedError
from domain.production.ports import JobFailed, JobSucceeded, VideoGenerator, VideoRequest
from domain.production.prompting import DEFAULT_VIDEO_MOTION, build_video_prompt
from infrastructure.providers.fal_queue import QUEUE_BASE_URL, FalQueueClient
from infrastructure.providers.fal_seedance_video import (
    SEEDANCE_ENDPOINT,
    FalPreparedVideoRequest,
    FalSeedanceVideoGenerator,
    build_seedance_payload,
)
from infrastructure.providers.fal_storage import CDN_UPLOAD_URL, STORAGE_TOKEN_URL, FalStorageClient
from tests.support.production import BytesDestination

BASE = f"{QUEUE_BASE_URL}/{SEEDANCE_ENDPOINT}/requests/r1"
RAW = VideoRequest(
    prompt="pot",
    source_image=b"PNG",
    source_image_mime="image/png",
    duration_ms=4000,
    aspect="9:16",
)


def _prepared(duration_ms: int = 4000) -> FalPreparedVideoRequest:
    return FalPreparedVideoRequest(
        prompt="pot",
        source_image=b"PNG",
        source_image_mime="image/png",
        duration_ms=duration_ms,
        aspect="9:16",
        image_url="https://v3.fal.media/f/x",
    )


def _generator(routes) -> tuple[FalSeedanceVideoGenerator, list[str]]:
    calls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(f"{request.method} {request.url}")
        return routes[str(request.url)]

    transport = httpx.MockTransport(handler)
    return (
        FalSeedanceVideoGenerator(
            FalQueueClient("k", transport=transport), FalStorageClient("k", transport=transport)
        ),
        calls,
    )


def test_payload_matches_the_verified_spec() -> None:
    payload = build_seedance_payload(_prepared(15000))
    assert payload == {
        "prompt": "pot",
        "image_url": "https://v3.fal.media/f/x",
        "duration": "15",
        "aspect_ratio": "9:16",
        "resolution": "720p",
        "generate_audio": False,
    }
    assert "seed" not in payload and "camera_fixed" not in payload


@pytest.mark.parametrize("ms", [3000, 4500, 16000])
def test_payload_rejects_unsupported_durations(ms) -> None:
    with pytest.raises(ProviderRejectedError):
        build_seedance_payload(_prepared(ms))


@pytest.mark.parametrize(
    ("requested", "supported"), [(500, 4000), (4000, 4000), (4001, 5000), (20000, 15000)]
)
def test_duration_clamp(requested, supported) -> None:
    gen, _ = _generator({})
    assert gen.supported_duration_ms(requested) == supported


def test_identity_and_cost() -> None:
    gen, _ = _generator({})
    assert isinstance(gen, VideoGenerator)
    assert gen.generation_profile_id == "fal-seedance-2.0-fast:720p:9x16:noaudio:v1"
    assert gen.estimate_cost_usd(RAW) == pytest.approx(4 * 0.2419)
    assert gen.estimate_cost_usd(_prepared(15000)) == pytest.approx(15 * 0.2419)


async def test_submit_requires_prepare_and_prepare_uploads() -> None:
    routes = {
        STORAGE_TOKEN_URL: httpx.Response(200, json={"token": "t", "token_type": "Bearer"}),
        CDN_UPLOAD_URL: httpx.Response(200, json={"access_url": "https://v3.fal.media/f/x"}),
        f"{QUEUE_BASE_URL}/{SEEDANCE_ENDPOINT}": httpx.Response(
            200, json={"request_id": "r1", "status_url": f"{BASE}/status", "response_url": BASE}
        ),
    }
    gen, calls = _generator(routes)
    with pytest.raises(ProviderRejectedError):
        await gen.submit(RAW)
    assert calls == []
    prepared = await gen.prepare(RAW)
    assert prepared.image_url == "https://v3.fal.media/f/x"
    ref = await gen.submit(prepared)
    assert json.loads(ref)["request_id"] == "r1"
    assert await gen.prepare(prepared) is prepared


async def test_poll_download_and_failures() -> None:
    ref = json.dumps(
        {
            "v": 1,
            "endpoint": SEEDANCE_ENDPOINT,
            "request_id": "r1",
            "status_url": f"{BASE}/status",
            "response_url": BASE,
            "cancel_url": None,
        }
    )
    routes = {
        f"{BASE}/status": httpx.Response(200, json={"status": "COMPLETED"}),
        BASE: httpx.Response(200, json={"video": {"url": "https://cdn.example/v.mp4"}, "seed": 3}),
        "https://cdn.example/v.mp4": httpx.Response(200, content=b"MP4"),
    }
    gen, _ = _generator(routes)
    assert isinstance(await gen.poll(ref), JobSucceeded)  # type: ignore[arg-type]
    dest = BytesDestination()
    await gen.download(ref, dest)  # type: ignore[arg-type]
    assert dest.data == b"MP4"
    evidence = await gen.describe_result(ref)  # type: ignore[arg-type]
    assert "url" not in json.dumps(evidence) and evidence["result"]["seed"] == 3

    for body, rejected in [
        ({"error": "x", "error_type": "content_policy_violation"}, True),
        ({"video": {}}, False),
    ]:
        routes[BASE] = httpx.Response(200, json=body)
        gen, _ = _generator(routes)
        status = await gen.poll(ref)  # type: ignore[arg-type]
        assert isinstance(status, JobFailed) and status.rejected is rejected


def _scene(**overrides) -> StoryboardScene:
    fields = dict(
        scene_id="sb1",
        order=1,
        script_scene_id="s1",
        start_ms=0,
        duration_ms=4000,
        visual_kind=StoryboardVisualKind.BROLL,
        visual_description="clay pot over fire",
        framing="close-up",
        camera_movement="slow push in",
        transition_in="fade",
    )
    fields.update(overrides)
    return StoryboardScene(**fields)  # type: ignore[arg-type]


def test_video_prompt_builder() -> None:
    prompt = build_video_prompt(_scene())
    assert prompt == build_video_prompt(_scene())
    assert prompt.startswith("clay pot over fire.")
    assert "camera: slow push in" in prompt and "opening: fade" in prompt
    assert "close-up" not in prompt  # 構図は元画像が持つ
    assert build_video_prompt(_scene(camera_movement=None, transition_in=None)) != prompt
    assert DEFAULT_VIDEO_MOTION.motion_profile_id.endswith(":video-prompt-v1")
