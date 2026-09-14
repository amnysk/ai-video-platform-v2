"""``RenderEngine`` / ``FinalVideoProbe`` のフェイク（tests/support/fakes.py と同じ作法）。"""

from __future__ import annotations

import asyncio
import dataclasses
from collections.abc import Callable
from pathlib import Path
from typing import Any

from contracts.render import RenderEngineIdentity, RenderPlan
from domain.render.ports import FinalVideoInfo, RenderedVideo, RenderRequest
from tests.support.render_plans import FAKE_ENGINE


class FakeRenderEngine:
    """呼び出しを記録し、``output_path`` へ ``payload`` を書く。

    - ``delay_seconds``: 書く前に待つ（その間に cancel されれば ``cancelled`` を立てて再送出）
    - ``failures``: 先頭から順に投げる例外（尽きたら成功する）。retry の検証用
    """

    def __init__(
        self,
        *,
        identity: RenderEngineIdentity = FAKE_ENGINE,
        payload: bytes = b"fake-final-video",
        delay_seconds: float = 0.0,
        failures: list[BaseException] | None = None,
        on_render: Callable[[RenderRequest], None] | None = None,
    ) -> None:
        self._identity = identity
        self.payload = payload
        self.delay_seconds = delay_seconds
        self.failures = list(failures or [])
        self.on_render = on_render
        self.requests: list[RenderRequest] = []
        self.heartbeats = 0
        self.cancelled = False
        self.completed = 0

    def identity(self) -> RenderEngineIdentity:
        return self._identity

    async def render(
        self, request: RenderRequest, *, heartbeat: Callable[[], object]
    ) -> RenderedVideo:
        self.requests.append(request)
        heartbeat()
        self.heartbeats += 1
        if self.on_render is not None:
            self.on_render(request)
        try:
            if self.delay_seconds:
                await asyncio.sleep(self.delay_seconds)
        except asyncio.CancelledError:
            self.cancelled = True
            raise
        if self.failures:
            raise self.failures.pop(0)
        request.output_path.parent.mkdir(parents=True, exist_ok=True)
        request.output_path.write_bytes(self.payload)
        self.completed += 1
        return RenderedVideo(path=request.output_path, bytes=len(self.payload))


class FakeFinalVideoProbe:
    """計画と整合する ``FinalVideoInfo`` を返す。``overrides`` で任意の項目を崩せる。"""

    def __init__(self, plan: RenderPlan | None = None, **overrides: Any) -> None:
        self.plan = plan
        self.overrides = overrides
        self.calls: list[str] = []

    def probe_final_video(self, path: str) -> FinalVideoInfo:
        self.calls.append(path)
        if self.plan is None:
            raise AssertionError("FakeFinalVideoProbe.plan is not set")
        profile = self.plan.profile
        total = self.plan.total_duration_ms
        info = FinalVideoInfo(
            duration_ms=total,
            width=profile.width,
            height=profile.height,
            fps_millis=profile.fps_millis,
            frames_decoded=total * profile.fps_millis // 1_000_000,
            decode_errors=0,
            video_codec=profile.video.codec,
            pix_fmt=profile.video.pix_fmt,
            audio_present=True,
            audio_codec=profile.audio.codec,
            audio_sample_rate_hz=profile.audio.sample_rate_hz,
            audio_channels=profile.audio.channels,
            audio_duration_ms=total,
            bytes=Path(path).stat().st_size if Path(path).exists() else 1,
        )
        return dataclasses.replace(info, **self.overrides)
