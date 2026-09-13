"""Production テストの共通部品（ADR-0017）。

生成器の fake は**本物のメディアバイト列**を返す（Pillow の PNG / PyAV の wav・mp4）。
検査規則（``domain.production.media``）とデコーダ（``infrastructure.media``）を実際に通すため。
"""

from __future__ import annotations

import io
import itertools
import math
import struct
import uuid
from dataclasses import dataclass, field
from typing import Any

import av
from PIL import Image

from contracts.artifacts import (
    ScriptArtifact,
    StoryboardArtifact,
    build_script_artifact,
    build_storyboard_artifact,
    parse_script_artifact,
    parse_storyboard_artifact,
)
from domain.errors import ProviderSubmitAmbiguousError
from domain.production.ports import (
    ImageRequest,
    JobFailed,
    JobPending,
    JobStatus,
    JobSucceeded,
    MediaDestination,
    ProviderJobRef,
    VideoRequest,
)

SHA_A = "a" * 64
SHA_B = "b" * 64

# --------------------------------------------------------------------------- 本物のメディア


def make_png(width: int = 1080, height: int = 1920, color: tuple[int, int, int] = (40, 90, 160)):
    image = Image.new("RGB", (width, height), color)
    out = io.BytesIO()
    image.save(out, format="PNG")
    return out.getvalue()


def make_wav(duration_ms: int = 1000, sample_rate: int = 22_050, channels: int = 1) -> bytes:
    """PCM s16le の wav。numpy を使わずフレームの plane へ直接書く。"""
    out = io.BytesIO()
    layout = "mono" if channels == 1 else "stereo"
    total = sample_rate * duration_ms // 1000
    with av.open(out, mode="w", format="wav") as container:
        stream = container.add_stream("pcm_s16le", rate=sample_rate, layout=layout)
        chunk = 1024
        for offset in range(0, total, chunk):
            count = min(chunk, total - offset)
            body = b"".join(
                struct.pack(
                    "<h", int(math.sin(2 * math.pi * 440 * (offset + i) / sample_rate) * 8000)
                )
                * channels
                for i in range(count)
            )
            frame = av.AudioFrame(format="s16", layout=layout, samples=count)
            frame.planes[0].update(body)
            frame.sample_rate = sample_rate
            frame.pts = offset
            for packet in stream.encode(frame):
                container.mux(packet)
        for packet in stream.encode(None):
            container.mux(packet)
    return out.getvalue()


def make_mp4(duration_ms: int = 1000, width: int = 720, height: int = 1280, fps: int = 24) -> bytes:
    """H.264 / yuv420p の mp4。numpy を使わず Y/U/V plane へ直接書く。"""
    out = io.BytesIO()
    with av.open(out, mode="w", format="mp4") as container:
        stream = container.add_stream("libx264", rate=fps)
        stream.width = width
        stream.height = height
        stream.pix_fmt = "yuv420p"
        frames = fps * duration_ms // 1000
        for index in range(frames):
            frame = av.VideoFrame(width, height, "yuv420p")
            luma = (index * 7) % 235 + 16
            for plane, value in zip(frame.planes, (luma, 128, 128), strict=True):
                plane.update(bytes([value]) * plane.buffer_size)
            frame.pts = index
            for packet in stream.encode(frame):
                container.mux(packet)
        for packet in stream.encode(None):
            container.mux(packet)
    return out.getvalue()


@dataclass
class BytesDestination:
    """``MediaDestination`` のメモリ実装。"""

    chunks: list[bytes] = field(default_factory=list)

    async def write(self, chunk: bytes) -> None:
        self.chunks.append(chunk)

    @property
    def data(self) -> bytes:
        return b"".join(self.chunks)


# --------------------------------------------------------------------------- 非同期ジョブ型 fake


@dataclass
class _Job:
    request: Any
    polls: int = 0


class _FakeAsyncJobGenerator:
    """submit → pending を ``pending_polls`` 回 → 完了。失敗・曖昧 submit を注入できる。"""

    generator_id: str
    generation_profile_id: str

    def __init__(
        self,
        *,
        pending_polls: int = 1,
        fail_with: JobFailed | None = None,
        ambiguous_submit: bool = False,
        cost_usd: float = 0.05,
    ) -> None:
        self.pending_polls = pending_polls
        self.fail_with = fail_with
        self.ambiguous_submit = ambiguous_submit
        self.cost_usd = cost_usd
        self.submit_calls = 0
        self.poll_calls = 0
        self.download_calls = 0
        self._jobs: dict[str, _Job] = {}
        self._ids = itertools.count(1)

    def estimate_cost_usd(self, request: Any) -> float:
        return self.cost_usd

    async def submit(self, request: Any) -> ProviderJobRef:
        self.submit_calls += 1
        if self.ambiguous_submit:
            raise ProviderSubmitAmbiguousError("fake: submit outcome unknown")
        ref = f"fake-job-{next(self._ids)}-{uuid.uuid4().hex[:8]}"
        self._jobs[ref] = _Job(request=request)
        return ProviderJobRef(ref)

    async def poll(self, ref: ProviderJobRef) -> JobStatus:
        self.poll_calls += 1
        job = self._jobs[ref]
        job.polls += 1
        if job.polls <= self.pending_polls:
            return JobPending()
        if self.fail_with is not None:
            return self.fail_with
        return JobSucceeded()

    async def download(self, ref: ProviderJobRef, dest: MediaDestination) -> None:
        self.download_calls += 1
        job = self._jobs[ref]
        await dest.write(self._render(job.request))

    def _render(self, request: Any) -> bytes:  # pragma: no cover - overridden
        raise NotImplementedError


class FakeImageGenerator(_FakeAsyncJobGenerator):
    """既定では 1024x1820（ほぼ 9:16）の PNG を返し、正規化の経路を通す。"""

    def __init__(self, *, output_size: tuple[int, int] = (1024, 1820), **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self.generator_id = "fake-image"
        self.generation_profile_id = "fake-image-profile-v1"
        self.output_size = output_size

    def _render(self, request: ImageRequest) -> bytes:
        return make_png(*self.output_size)


class FakeVideoGenerator(_FakeAsyncJobGenerator):
    """provider の尺の丸め（1秒刻み）を模す。720x1280 / 24fps の mp4 を返す。"""

    def __init__(self, *, fps: int = 24, size: tuple[int, int] = (720, 1280), **kwargs: Any):
        super().__init__(**kwargs)
        self.generator_id = "fake-video"
        self.generation_profile_id = "fake-video-profile-v1"
        self.fps = fps
        self.size = size

    def supported_duration_ms(self, requested_ms: int) -> int:
        return max(1000, round(requested_ms / 1000) * 1000)

    def _render(self, request: VideoRequest) -> bytes:
        return make_mp4(request.duration_ms, self.size[0], self.size[1], self.fps)


class FakeVoiceGenerator:
    """同期・ローカルの音声合成。1文字あたり ``ms_per_char`` の wav を返す。"""

    def __init__(self, *, ms_per_char: int = 60, fail_times: int = 0) -> None:
        self.generator_id = "fake-voice"
        self.generation_profile_id = "fake-voice-profile-v1"
        self.voice_id = "fake-voice-ja"
        self.ms_per_char = ms_per_char
        self.fail_times = fail_times
        self.calls = 0

    async def synthesize(self, text: str, language: str, dest: MediaDestination) -> None:
        self.calls += 1
        if self.calls <= self.fail_times:
            raise RuntimeError("fake: voice synthesis crashed")
        duration = min(60_000, max(500, len(text) * self.ms_per_char))
        await dest.write(make_wav(duration))


# --------------------------------------------------------------------------- 入力 Artifact

SCRIPT_SCENES = [
    {"id": "s1", "narration": "縄文土器には焦げ跡が残る。", "visual": "土器", "duration_ms": 8000},
    {"id": "s2", "narration": "煮炊きに使われた証拠だ。", "visual": "炉", "duration_ms": 9000},
    {"id": "s3", "narration": "食が定住を支えた。", "visual": "集落", "duration_ms": 8000},
]


def sample_script(episode_id: str) -> ScriptArtifact:
    return parse_script_artifact(
        build_script_artifact(
            episode_id=episode_id,
            language="ja",
            title="縄文の食",
            hook="土器が語る",
            scenes=SCRIPT_SCENES,
            metadata={"topic": "縄文", "generator": "fake", "generator_model": "fake"},
        )
    )


def sample_storyboard(
    episode_id: str, *, script_artifact_id: str | None = None, script_sha256: str = SHA_A
) -> StoryboardArtifact:
    """s2 を2シーンに分けた storyboard（sb1=s1, sb2/sb3=s2, sb4=s3）。"""
    scenes = [
        ("s1", 0, 8000, "broll", "土器のクローズアップ"),
        ("s2", 8000, 4000, "animation", "炉に火を入れる"),
        ("s2", 12000, 5000, "animation", "煮炊きする再現"),
        ("s3", 17000, 8000, "diagram", "集落の俯瞰図"),
    ]
    return parse_storyboard_artifact(
        build_storyboard_artifact(
            episode_id=episode_id,
            source_script={
                "artifact_id": script_artifact_id or str(uuid.uuid4()),
                "sha256": script_sha256,
                "schema_version": "1.0",
            },
            scenes=[
                {
                    "scene_id": f"sb{i}",
                    "order": i,
                    "script_scene_id": sid,
                    "start_ms": start,
                    "duration_ms": duration,
                    "visual_kind": kind,
                    "visual_description": desc,
                    "camera_movement": "slow push in" if i == 2 else None,
                }
                for i, (sid, start, duration, kind, desc) in enumerate(scenes, start=1)
            ],
            total_duration_ms=25000,
            metadata={
                "generator": "fake",
                "generator_model": "fake",
                "generation_spec_id": "spec-1",
            },
        )
    )


def media_descriptor(
    mime: str = "image/png", *, sha256: str = SHA_A, size: int = 1234
) -> dict[str, Any]:
    ext = {"image/png": "png", "audio/wav": "wav", "video/mp4": "mp4"}.get(mime, "bin")
    return {
        "object_key": f"media/ep/scene_image/sb1/{sha256}.{ext}",
        "sha256": sha256,
        "bytes": size,
        "mime": mime,
    }


GENERATOR = {
    "generator": "fake-image",
    "generator_model": "fake-model",
    "generation_profile_id": "fake-image-profile-v1",
}


def source_ref(sha256: str = SHA_A) -> dict[str, str]:
    return {"artifact_id": str(uuid.uuid4()), "sha256": sha256, "schema_version": "1.0"}


def digest_ref(sha256: str = SHA_B) -> dict[str, str]:
    return {"artifact_id": str(uuid.uuid4()), "sha256": sha256}
