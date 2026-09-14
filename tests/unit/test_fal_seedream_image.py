"""Seedream adapter の payload と結果の写像、prompt builder（ADR-0017）。"""

from __future__ import annotations

import json

import httpx
import pytest

from contracts.artifacts import StoryboardScene, StoryboardVisualKind
from domain.errors import ProviderRejectedError
from domain.production.ports import (
    ImageGenerator,
    ImageRequest,
    JobFailed,
    JobPending,
    JobSucceeded,
)
from domain.production.prompting import DEFAULT_IMAGE_STYLE, build_image_prompt
from infrastructure.providers.fal_queue import QUEUE_BASE_URL, FalQueueClient
from infrastructure.providers.fal_seedream_image import (
    SEEDREAM_ENDPOINT,
    SEEDREAM_PROFILE_ID,
    FalSeedreamImageGenerator,
    build_seedream_payload,
)
from tests.support.production import BytesDestination

REQUEST = ImageRequest(prompt="a pot", width=1080, height=1920, aspect="9:16")
BASE = f"{QUEUE_BASE_URL}/{SEEDREAM_ENDPOINT}/requests/r1"


def test_payload_matches_the_verified_spec() -> None:
    assert build_seedream_payload(REQUEST) == {
        "prompt": "a pot",
        "image_size": {"width": 2160, "height": 3840},
        "num_images": 1,
        "max_images": 1,
        "enable_safety_checker": True,
        "sync_mode": False,
    }
    assert "seed" not in build_seedream_payload(REQUEST)


def test_non_vertical_requests_are_rejected() -> None:
    with pytest.raises(ProviderRejectedError):
        build_seedream_payload(ImageRequest(prompt="x", width=1920, height=1080, aspect="16:9"))


def test_identity_and_cost_are_stable() -> None:
    gen = FalSeedreamImageGenerator(
        FalQueueClient("k", transport=httpx.MockTransport(lambda r: httpx.Response(500)))
    )
    assert isinstance(gen, ImageGenerator)
    assert (
        gen.generation_profile_id
        == SEEDREAM_PROFILE_ID
        == "fal-seedream-4.5:2160x3840:safety-on:v1"
    )
    assert gen.estimate_cost_usd(REQUEST) == pytest.approx(0.04)


def _generator(routes: dict[str, httpx.Response]) -> tuple[FalSeedreamImageGenerator, list[str]]:
    calls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        calls.append(f"{request.method} {url}")
        return routes[url]

    return FalSeedreamImageGenerator(
        FalQueueClient("k", transport=httpx.MockTransport(handler))
    ), calls


async def test_submit_poll_download_roundtrip() -> None:
    routes = {
        f"{QUEUE_BASE_URL}/{SEEDREAM_ENDPOINT}": httpx.Response(
            200, json={"request_id": "r1", "status_url": f"{BASE}/status", "response_url": BASE}
        ),
        f"{BASE}/status": httpx.Response(200, json={"status": "IN_PROGRESS"}),
        BASE: httpx.Response(
            200, json={"images": [{"url": "https://cdn.example/i.png", "width": 2160}], "seed": 7}
        ),
        "https://cdn.example/i.png": httpx.Response(200, content=b"PNGDATA"),
    }
    gen, calls = _generator(routes)
    ref = await gen.submit(REQUEST)
    assert json.loads(ref)["request_id"] == "r1"
    assert isinstance(await gen.poll(ref), JobPending)
    routes[f"{BASE}/status"] = httpx.Response(200, json={"status": "COMPLETED"})
    assert isinstance(await gen.poll(ref), JobSucceeded)
    dest = BytesDestination()
    await gen.download(ref, dest)
    assert dest.data == b"PNGDATA"
    evidence = await gen.describe_result(ref)
    assert "url" not in json.dumps(evidence)
    assert evidence["result"]["seed"] == 7
    assert sum(1 for c in calls if c == f"GET {BASE}") == 1  # 結果は1回だけ取る


@pytest.mark.parametrize(
    ("body", "rejected"),
    [
        ({"error": "nsfw", "error_type": "content_policy_violation"}, True),
        ({"error": "crash", "error_type": "runner_server_error"}, False),
        ({"images": []}, False),
    ],
)
async def test_completed_failures_become_job_failed(body, rejected) -> None:
    routes = {
        f"{BASE}/status": httpx.Response(200, json={"status": "COMPLETED"}),
        BASE: httpx.Response(200, json=body),
    }
    gen, _ = _generator(routes)
    ref = json.dumps(
        {
            "v": 1,
            "endpoint": SEEDREAM_ENDPOINT,
            "request_id": "r1",
            "status_url": f"{BASE}/status",
            "response_url": BASE,
            "cancel_url": None,
        }
    )
    status = await gen.poll(ref)  # type: ignore[arg-type]
    assert isinstance(status, JobFailed)
    assert status.rejected is rejected


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
    )
    fields.update(overrides)
    return StoryboardScene(**fields)  # type: ignore[arg-type]


def test_prompt_builder_is_deterministic_and_ignores_motion() -> None:
    prompt = build_image_prompt(_scene())
    assert prompt == build_image_prompt(_scene())
    assert prompt.startswith("clay pot over fire.")
    assert "framing: close-up" in prompt
    assert "push in" not in prompt
    assert "no text" in prompt
    assert build_image_prompt(_scene(camera_movement=None)) == prompt
    assert build_image_prompt(_scene(framing=None)) != prompt


def test_every_visual_kind_has_a_hint() -> None:
    for kind in StoryboardVisualKind:
        assert build_image_prompt(_scene(visual_kind=kind))


def test_style_profile_id_carries_the_builder_version() -> None:
    assert DEFAULT_IMAGE_STYLE.style_profile_id.endswith(":prompt-v1")
