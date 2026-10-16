"""完成動画の input_hash（ADR-0019 §5）。"""

from __future__ import annotations

import inspect
from collections.abc import Callable
from typing import Any

import pytest

from contracts.render import RenderEngineIdentity, TimelinePolicy, get_render_profile
from domain.render import identity
from domain.render.audio import AudioMixSpec, audio_mix_spec
from domain.render.identity import render_input_hash

PROFILE = get_render_profile("shorts_vertical")
BASE: dict[str, Any] = {
    "manifest_sha256": "1" * 64,
    "script_sha256": "2" * 64,
    "storyboard_sha256": "3" * 64,
    "profile": PROFILE,
    "policy": TimelinePolicy(),
    "engine": RenderEngineIdentity(engine="eng", version="7.1.1", binary_sha256="4" * 64),
    "font_sha256": "5" * 64,
}


def test_hash_is_stable() -> None:
    assert render_input_hash(**BASE) == render_input_hash(**dict(BASE))
    assert render_input_hash(**BASE) == render_input_hash(**BASE, audio_mix=audio_mix_spec(PROFILE))


def _subs(**over: Any) -> Any:
    return PROFILE.model_copy(update={"subtitles": PROFILE.subtitles.model_copy(update=over)})


CHANGES: dict[str, Callable[[dict[str, Any]], None]] = {
    "manifest": lambda b: b.update(manifest_sha256="9" * 64),
    "script": lambda b: b.update(script_sha256="9" * 64),
    "storyboard": lambda b: b.update(storyboard_sha256="9" * 64),
    "profile_id": lambda b: b.update(profile=get_render_profile("long_form_horizontal")),
    "crf": lambda b: b.update(
        profile=PROFILE.model_copy(update={"video": PROFILE.video.model_copy(update={"crf": 23})})
    ),
    "subtitle_enabled": lambda b: b.update(profile=_subs(enabled=False)),
    "subtitle_line_length": lambda b: b.update(profile=_subs(max_chars_per_line=20)),
    "max_freeze": lambda b: b.update(policy=TimelinePolicy(max_freeze_ms=1000)),
    "template_version": lambda b: b.update(policy=TimelinePolicy(template_version=99)),
    "engine_version": lambda b: b.update(
        engine=BASE["engine"].model_copy(update={"version": "7.1.2"})
    ),
    "engine_binary": lambda b: b.update(
        engine=BASE["engine"].model_copy(update={"binary_sha256": "8" * 64})
    ),
    "font": lambda b: b.update(font_sha256="9" * 64),
    "audio_mix_gain": lambda b: b.update(
        audio_mix=AudioMixSpec(sample_rate_hz=48_000, channels=2, gain_permille=900)
    ),
}


@pytest.mark.parametrize("name", sorted(CHANGES))
def test_hash_is_sensitive_to(name: str) -> None:
    changed = dict(BASE)
    CHANGES[name](changed)
    assert render_input_hash(**changed) != render_input_hash(**BASE)


def test_hash_has_no_attempt_job_run_time_or_path_inputs() -> None:
    params = set(inspect.signature(render_input_hash).parameters)
    forbidden = {"attempt", "job_id", "run_id", "workflow_id", "created_at", "path", "font_path"}
    assert not params & forbidden
    source = inspect.getsource(identity)
    assert "datetime" not in source
    assert "time.time" not in source


def test_audio_mix_is_fixed_gain_without_normalization() -> None:
    mix = audio_mix_spec(PROFILE)
    assert (mix.sample_rate_hz, mix.channels) == (48_000, 2)
    assert (mix.gain_permille, mix.normalize, mix.loudness_normalization) == (1000, False, False)


def test_hash_is_sensitive_to_engine_threads() -> None:
    assert render_input_hash(**BASE, engine_threads=4) != render_input_hash(
        **BASE, engine_threads=8
    )
    assert render_input_hash(**BASE, engine_threads=4) == render_input_hash(
        **BASE, engine_threads=4
    )
