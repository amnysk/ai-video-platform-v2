"""production の土台: 遷移・キー規約・ストア・fake・Activity 契約（ADR-0017 / ADR-0018）。"""

from __future__ import annotations

import dataclasses

import pytest

from contracts import production_activities as pa
from contracts.states import (
    PRODUCTION_IMAGE_TASK_QUEUE,
    PRODUCTION_VIDEO_TASK_QUEUE,
    PRODUCTION_VOICE_TASK_QUEUE,
    PRODUCTION_WORKFLOW,
    EpisodeStatus,
    ProviderCall,
)
from domain.artifact.hashing import sha256_hex
from domain.artifact.keys import artifact_object_key, media_object_key
from domain.episode.transitions import EpisodeEvent, Rejected, transition_episode
from domain.errors import ArtifactConflictError, ProviderSubmitAmbiguousError
from domain.production.ports import (
    ImageGenerator,
    ImageRequest,
    JobFailed,
    JobPending,
    JobSucceeded,
    MediaDestination,
    VideoGenerator,
    VideoRequest,
    VoiceGenerator,
)
from infrastructure.config import Settings
from infrastructure.media.probe import PillowAvMediaProbe
from infrastructure.storage.artifact_store import ArtifactStore, readback_sha256
from tests.support.production import (
    BytesDestination,
    FakeImageGenerator,
    FakeVideoGenerator,
    FakeVoiceGenerator,
    make_png,
)

# --------------------------------------------------------------------------- 遷移


def test_production_happy_path_parks_at_assets_ready() -> None:
    s = transition_episode(EpisodeStatus.STORYBOARD_READY, EpisodeEvent.STAGE_ADMITTED)
    assert s is EpisodeStatus.IN_PROGRESS
    s = transition_episode(s, EpisodeEvent.ASSETS_READY)
    assert s is EpisodeStatus.ASSETS_READY
    assert transition_episode(s, EpisodeEvent.STAGE_ADMITTED) is EpisodeStatus.IN_PROGRESS
    assert transition_episode(s, EpisodeEvent.CANCELLED) is EpisodeStatus.CANCELLED


@pytest.mark.parametrize(
    ("current", "event"),
    [
        (EpisodeStatus.STORYBOARD_READY, EpisodeEvent.ASSETS_READY),  # 工程を飛ばせない
        (EpisodeStatus.SCRIPT_READY, EpisodeEvent.ASSETS_READY),
        (EpisodeStatus.ASSETS_READY, EpisodeEvent.ASSETS_READY),
        (EpisodeStatus.ASSETS_READY, EpisodeEvent.STORYBOARD_READY),
    ],
)
def test_invalid_production_transitions_are_rejected(current, event) -> None:
    assert isinstance(transition_episode(current, event), Rejected)


def test_vocabulary_constants() -> None:
    assert PRODUCTION_WORKFLOW == ("ProductionWorkflow", "production")
    assert (
        PRODUCTION_IMAGE_TASK_QUEUE,
        PRODUCTION_VOICE_TASK_QUEUE,
        PRODUCTION_VIDEO_TASK_QUEUE,
    ) == (
        "production-image",
        "production-voice",
        "production-video",
    )
    assert {ProviderCall.FAL_IMAGE.value, ProviderCall.FAL_VIDEO.value} <= {
        p.value for p in ProviderCall
    }
    assert not any("piper" in p.value for p in ProviderCall)  # ローカル TTS は台帳に載せない


# --------------------------------------------------------------------------- キー規約


def test_artifact_key_without_scene_is_unchanged() -> None:
    assert artifact_object_key("ep", "script", "s" * 64) == f"artifacts/ep/script/{'s' * 64}.json"


def test_scene_artifact_and_media_keys() -> None:
    sha = "0" * 64
    assert artifact_object_key("ep", "scene_image", sha, "sb1") == (
        f"artifacts/ep/scene_image/sb1/{sha}.json"
    )
    assert media_object_key("ep", "scene_video", "sb2", sha, "mp4") == (
        f"media/ep/scene_video/sb2/{sha}.mp4"
    )


@pytest.mark.parametrize(
    "args",
    [
        ("ep", "scene_image", "../x", "0" * 64, "png"),
        ("ep/1", "scene_image", "sb1", "0" * 64, "png"),
        ("ep", "scene_image", "sb1", "XYZ", "png"),
        ("ep", "scene_image", "sb1", "0" * 64, "p.ng"),
    ],
)
def test_media_key_rejects_unsafe_segments(args) -> None:
    with pytest.raises(ValueError):
        media_object_key(*args)


# --------------------------------------------------------------------------- ストア


async def test_memory_store_bytes_roundtrip_and_immutability(artifact_store) -> None:
    assert isinstance(artifact_store, ArtifactStore)
    data = make_png(10, 10)
    key = f"media/ep/scene_image/sb1/{sha256_hex(data)}.png"
    put = await artifact_store.put_bytes(key, data, "image/png")
    assert put.sha256 == sha256_hex(data) and not put.existed
    assert (await artifact_store.put_bytes(key, data, "image/png")).existed
    assert await artifact_store.get_bytes(key) == data
    assert await readback_sha256(artifact_store, key) == put.sha256
    stat = await artifact_store.stat(key)
    assert stat.size == len(data) and stat.content_type == "image/png" and stat.etag
    with pytest.raises(ArtifactConflictError):
        await artifact_store.put_bytes(key, b"other", "image/png")
    with pytest.raises(KeyError):
        await artifact_store.stat("missing")


async def test_memory_store_accepts_a_path(artifact_store, tmp_path) -> None:
    path = tmp_path / "x.bin"
    path.write_bytes(b"abc")
    put = await artifact_store.put_bytes("media/k", path, "application/octet-stream")
    assert put.size == 3


# --------------------------------------------------------------------------- fake


def test_fakes_satisfy_the_ports() -> None:
    assert isinstance(FakeImageGenerator(), ImageGenerator)
    assert isinstance(FakeVideoGenerator(), VideoGenerator)
    assert isinstance(FakeVoiceGenerator(), VoiceGenerator)
    assert isinstance(BytesDestination(), MediaDestination)


async def test_fake_image_job_simulates_pending_then_done() -> None:
    gen = FakeImageGenerator(pending_polls=2)
    ref = await gen.submit(ImageRequest(prompt="p", width=1080, height=1920, aspect="9:16"))
    assert [await gen.poll(ref) for _ in range(3)] == [JobPending(), JobPending(), JobSucceeded()]
    dest = BytesDestination()
    await gen.download(ref, dest)
    info = PillowAvMediaProbe().probe_image(dest.data)
    assert (info.width, info.height) == gen.output_size
    assert gen.submit_calls == 1 and gen.poll_calls == 3 and gen.download_calls == 1


async def test_fake_failure_and_ambiguous_submit_injection() -> None:
    failing = FakeVideoGenerator(pending_polls=0, fail_with=JobFailed("nope", rejected=True))
    request = VideoRequest("p", make_png(8, 8), "image/png", 1000, "9:16")
    ref = await failing.submit(request)
    assert await failing.poll(ref) == JobFailed("nope", rejected=True)

    ambiguous = FakeImageGenerator(ambiguous_submit=True)
    with pytest.raises(ProviderSubmitAmbiguousError):
        await ambiguous.submit(ImageRequest("p", 1080, 1920, "9:16"))
    assert ambiguous.submit_calls == 1


async def test_fake_video_and_voice_emit_valid_media() -> None:
    video = FakeVideoGenerator(pending_polls=0)
    duration = video.supported_duration_ms(1200)
    ref = await video.submit(VideoRequest("p", make_png(8, 8), "image/png", duration, "9:16"))
    dest = BytesDestination()
    await video.download(ref, dest)
    info = PillowAvMediaProbe().probe_video(dest.data)
    assert info.frames_decoded == 24 and (info.width, info.height) == (720, 1280)

    voice = FakeVoiceGenerator()
    dest = BytesDestination()
    await voice.synthesize("こんにちは", "ja", dest)
    assert PillowAvMediaProbe().probe_audio(dest.data).duration_ms >= 200


# --------------------------------------------------------------------------- Activity 契約


def test_activity_names_are_unique_and_prefixed() -> None:
    names = pa.PRODUCTION_ACTIVITY_NAMES
    assert len(names) == len(set(names)) == 10
    assert all(n.split("_")[0] in {"production", "image", "voice", "video"} for n in names)


def test_execution_policy_constants() -> None:
    assert pa.SUBMIT_MAX_ATTEMPTS == 1
    assert pa.AWAIT_MAX_ATTEMPTS == 5
    assert pa.AWAIT_START_TO_CLOSE_SECONDS == 40 * 60
    assert pa.AWAIT_HEARTBEAT_TIMEOUT_SECONDS == 90
    assert pa.VOICE_MAX_ATTEMPTS == 3


def _dataclasses() -> list[type]:
    return [v for v in vars(pa).values() if isinstance(v, type) and dataclasses.is_dataclass(v)]


def test_activity_payloads_do_not_carry_provider_job_refs_or_paths() -> None:
    for cls in _dataclasses():
        for f in dataclasses.fields(cls):
            assert "job_ref" not in f.name and "path" not in f.name, (cls.__name__, f.name)


async def test_activity_payloads_roundtrip_through_temporal_converter() -> None:
    from temporalio.converter import DataConverter

    converter = DataConverter.default
    samples = [
        pa.SubmitResult(
            reservation_id="r",
            artifact=pa.SceneArtifactResult(
                artifact_id="a", object_key="k", sha256="s", reused=True
            ),
        ),
        pa.ProductionPlan(
            storyboard_artifact_id="sb",
            storyboard_sha256="x",
            script_artifact_id="sc",
            script_sha256="y",
            images=[pa.SceneImageWork(scene_id="sb1")],
            videos=[pa.SceneVideoWork(scene_id="sb1", requested_duration_ms=5000)],
            voices=[pa.SceneVoiceWork(script_scene_id="s1", storyboard_scene_ids=["sb1"])],
        ),
        pa.VideoAwaitRequest("e", "w", "r", "sb1", "sb", "img", 5000, "res"),
    ]
    for sample in samples:
        payloads = await converter.encode([sample])
        (decoded,) = await converter.decode(payloads, [type(sample)])
        assert decoded == sample


def test_settings_defaults_for_production() -> None:
    settings = Settings(_env_file=None)  # type: ignore[call-arg]
    assert (settings.image_concurrency, settings.voice_concurrency, settings.video_concurrency) == (
        2,
        1,
        1,
    )
    assert settings.production_await_timeout_seconds == 2400
    assert settings.production_await_heartbeat_seconds == 90
    assert "fal" not in repr(settings).lower()
