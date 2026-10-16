"""本物の Piper で1文を合成して検査規則を通す（Phase 4B）。

ローカル・非課金だが、隔離 venv と音声モデルが要るので ``AVP_LIVE_PIPER=1`` のときだけ収集する。

    ./scripts/setup-piper.sh
    AVP_LIVE_PIPER=1 pytest tests/live/test_piper_voice_live.py -m live -s
"""

from __future__ import annotations

import os
import time
from pathlib import Path

import pytest

from domain.production.media import validate_voice
from infrastructure.media.destination import FileMediaDestination
from infrastructure.media.probe import PillowAvMediaProbe
from infrastructure.providers.piper_voice import PiperVoiceGenerator
from infrastructure.providers.process import SubprocessRunner

PIPER_HOME = Path(os.environ.get("PIPER_HOME", Path.home() / ".local/share/avp2/piper"))
PYTHON = os.environ.get("PIPER_PYTHON") or str(PIPER_HOME / "venv/bin/python")
VOICE = os.environ.get("PIPER_VOICE_PATH") or str(PIPER_HOME / "voices/en_US-kristin-medium.onnx")


@pytest.mark.live
async def test_real_piper_synthesizes_a_valid_voice(tmp_path) -> None:
    generator = await PiperVoiceGenerator.load(
        python=PYTHON, model_path=VOICE, runner=SubprocessRunner(), timeout_seconds=120
    )
    out = tmp_path / "voice.wav"
    started = time.monotonic()
    await generator.synthesize(
        "Jomon pottery still carries the scorch marks of ancient cooking fires.",
        "en",
        FileMediaDestination(out),
    )
    elapsed = time.monotonic() - started
    data = out.read_bytes()
    info = PillowAvMediaProbe().probe_audio(data)
    validate_voice(info, len(data))
    print(
        f"\npiper live: profile={generator.generation_profile_id} "
        f"duration_ms={info.duration_ms} sample_rate={info.sample_rate_hz} "
        f"channels={info.channels} bytes={len(data)} synth_seconds={elapsed:.2f}"
    )
    assert info.sample_rate_hz == 22050 and info.channels == 1
    assert 1500 <= info.duration_ms <= 10_000
