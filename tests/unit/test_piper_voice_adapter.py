"""PiperVoiceGenerator の検査（Phase 4B）。

本物の piper は使わない（INV-18）。``piper_cli/synthesize.py`` と同じ入出力規約の
偽スクリプトを、本物の ``SubprocessRunner`` で起動する。
"""

from __future__ import annotations

import json
import sys
import textwrap
from pathlib import Path

import pytest

from domain.errors import (
    ProviderInvocationError,
    ProviderTimeoutError,
    ProviderUnavailableError,
    VoiceLanguageUnsupportedError,
)
from infrastructure.media.destination import FileMediaDestination
from infrastructure.media.probe import PillowAvMediaProbe
from infrastructure.providers.piper_voice import (
    SYNTHESIZE_SCRIPT,
    PiperVoiceGenerator,
    PiperVoiceProfile,
)
from infrastructure.providers.process import ProcessResult, SubprocessRunner
from tests.support.production import BytesDestination

FAKE_SCRIPT = textwrap.dedent(
    """
    import json, os, sys, time, wave
    if sys.argv[1:] == ["--version"]:
        if os.environ.get("AVP_FAKE_SECRET"):
            sys.exit(9)  # 親の環境変数を受け取っていたら失敗させる
        print(json.dumps({"piper_tts": "1.8.0"}))
        sys.exit(0)
    req = json.loads(sys.stdin.read())
    with open(req["output_path"] + ".args.json", "w") as f:
        json.dump(req, f)
    text = req["text"]
    if text == "UNAVAILABLE":
        sys.stderr.write("cannot load piper voice")
        sys.exit(3)
    if text == "CRASH":
        sys.stderr.write("x" * 5000 + "TAIL")
        sys.exit(1)
    if text == "SLEEP":
        time.sleep(30)
    if text == "EMPTY":
        sys.exit(0)
    with wave.open(req["output_path"], "wb") as w:
        w.setnchannels(1); w.setsampwidth(2); w.setframerate(22050)
        w.writeframes(b"\\x10\\x00" * 22050)
    """
)


@pytest.fixture
def voice_files(tmp_path: Path) -> tuple[Path, Path]:
    script = tmp_path / "fake_synthesize.py"
    script.write_text(FAKE_SCRIPT, encoding="utf-8")
    model = tmp_path / "en_US-test-medium.onnx"
    model.write_bytes(b"fake onnx model")
    model.with_name(model.name + ".json").write_text(
        json.dumps(
            {
                "language": {"family": "en", "code": "en_US"},
                "audio": {"sample_rate": 22050},
                "inference": {"noise_scale": 0.667, "length_scale": 1, "noise_w": 0.8},
            }
        ),
        encoding="utf-8",
    )
    return script, model


async def _load(voice_files, **kwargs) -> PiperVoiceGenerator:
    script, model = voice_files
    return await PiperVoiceGenerator.load(
        python=sys.executable,
        model_path=model,
        runner=kwargs.pop("runner", SubprocessRunner(grace_seconds=0.2)),
        timeout_seconds=kwargs.pop("timeout_seconds", 10),
        script=script,
        **kwargs,
    )


async def test_load_builds_profile_from_version_model_sha_and_voice_defaults(voice_files) -> None:
    generator = await _load(voice_files)
    profile = generator.profile
    assert profile.piper_tts_version == "1.8.0"
    assert profile.voice_id == "en_US-test-medium"
    assert len(profile.model_sha256) == 64
    assert (profile.length_scale, profile.noise_scale, profile.noise_w_scale) == (1.0, 0.667, 0.8)
    assert generator.generator_id == "piper"
    assert generator.voice_id == "en_US-test-medium"
    assert generator.speed_permille == 1000
    assert generator.generation_profile_id.startswith("piper-1.8.0-en_US-test-medium-")
    assert len(generator.generation_profile_id) <= 128


async def test_profile_id_is_stable_and_changes_with_each_component(voice_files) -> None:
    first = await _load(voice_files)
    second = await _load(voice_files)
    assert first.generation_profile_id == second.generation_profile_id

    base = first.profile
    variants = [
        PiperVoiceProfile(**{**_fields(base), "piper_tts_version": "1.8.1"}),
        PiperVoiceProfile(**{**_fields(base), "model_sha256": "0" * 64}),
        PiperVoiceProfile(**{**_fields(base), "length_scale": 1.1}),
        PiperVoiceProfile(**{**_fields(base), "noise_scale": 0.0}),
        PiperVoiceProfile(**{**_fields(base), "noise_w_scale": 0.0}),
        PiperVoiceProfile(**{**_fields(base), "sample_rate_hz": 16000}),
    ]
    ids = {v.profile_id for v in variants} | {base.profile_id}
    assert len(ids) == len(variants) + 1


def _fields(profile: PiperVoiceProfile) -> dict:
    return {name: getattr(profile, name) for name in PiperVoiceProfile.__slots__}


async def test_settings_override_voice_defaults(voice_files) -> None:
    generator = await _load(voice_files, length_scale=1.25, noise_scale=0.0, noise_w_scale=0.0)
    assert generator.speed_permille == 800
    default = await _load(voice_files)
    assert generator.generation_profile_id != default.generation_profile_id


async def test_synthesize_writes_wav_and_sends_config_on_stdin(voice_files, tmp_path) -> None:
    generator = await _load(voice_files)
    out = tmp_path / "out.wav"
    await generator.synthesize("Hello there.", "en", FileMediaDestination(out))
    info = PillowAvMediaProbe().probe_audio(out.read_bytes())
    assert (info.duration_ms, info.sample_rate_hz, info.channels) == (1000, 22050, 1)
    sent = json.loads((tmp_path / "out.wav.args.json").read_text())
    assert sent["text"] == "Hello there."
    assert sent["model_path"].endswith("en_US-test-medium.onnx")
    assert sent["config_path"].endswith("en_US-test-medium.onnx.json")
    assert (sent["length_scale"], sent["noise_scale"], sent["noise_w_scale"]) == (1.0, 0.667, 0.8)
    assert generator.build_argv() == [sys.executable, str(voice_files[0])]


async def test_synthesize_to_a_non_file_destination_streams_bytes(voice_files) -> None:
    generator = await _load(voice_files)
    dest = BytesDestination()
    await generator.synthesize("Hello.", "en", dest)
    assert dest.data[:4] == b"RIFF"


async def test_child_does_not_inherit_secrets(voice_files, monkeypatch) -> None:
    monkeypatch.setenv("AVP_FAKE_SECRET", "leak")
    await _load(voice_files)  # 偽スクリプトは環境変数を受け取ると exit 9


async def test_unsupported_language_is_needs_input_without_starting_a_process(voice_files) -> None:
    class ExplodingRunner:
        calls = 0

        async def run(self, argv, *, stdin, env, timeout_seconds, cwd=None) -> ProcessResult:
            self.calls += 1
            if "--version" in argv:
                return ProcessResult(0, json.dumps({"piper_tts": "1.8.0"}), "")
            raise AssertionError("must not synthesize")

    runner = ExplodingRunner()
    generator = await _load(voice_files, runner=runner)
    with pytest.raises(VoiceLanguageUnsupportedError):
        await generator.synthesize("縄文土器", "ja", BytesDestination())
    assert runner.calls == 1
    assert generator.supports_language("en") and not generator.supports_language("ja")


@pytest.mark.parametrize(
    ("text", "error"),
    [
        ("UNAVAILABLE", ProviderUnavailableError),
        ("CRASH", ProviderInvocationError),
        ("EMPTY", ProviderInvocationError),
    ],
)
async def test_exit_codes_map_to_failure_classes(voice_files, tmp_path, text, error) -> None:
    generator = await _load(voice_files)
    with pytest.raises(error) as info:
        await generator.synthesize(text, "en", FileMediaDestination(tmp_path / "o.wav"))
    assert len(str(info.value)) < 2100


async def test_stderr_is_truncated_to_its_tail(voice_files, tmp_path) -> None:
    generator = await _load(voice_files)
    with pytest.raises(ProviderInvocationError) as info:
        await generator.synthesize("CRASH", "en", FileMediaDestination(tmp_path / "o.wav"))
    assert str(info.value).endswith("TAIL")


async def test_timeout_is_retryable_provider_timeout(voice_files, tmp_path) -> None:
    generator = await _load(voice_files)
    generator._timeout_seconds = 1  # noqa: SLF001 - 起動時の版確認は通したい
    with pytest.raises(ProviderTimeoutError):
        await generator.synthesize("SLEEP", "en", FileMediaDestination(tmp_path / "o.wav"))


async def test_missing_settings_or_files_are_unavailable(voice_files, tmp_path) -> None:
    script, model = voice_files
    runner = SubprocessRunner()
    cases = [
        {"python": None, "model_path": model},
        {"python": sys.executable, "model_path": None},
        {"python": tmp_path / "no-python", "model_path": model},
        {"python": sys.executable, "model_path": tmp_path / "missing.onnx"},
    ]
    for case in cases:
        with pytest.raises(ProviderUnavailableError):
            await PiperVoiceGenerator.load(runner=runner, timeout_seconds=5, script=script, **case)
    model.with_name(model.name + ".json").unlink()
    with pytest.raises(ProviderUnavailableError):
        await PiperVoiceGenerator.load(
            python=sys.executable, model_path=model, runner=runner, timeout_seconds=5, script=script
        )


async def test_interpreter_without_piper_is_unavailable(voice_files) -> None:
    """共有 venv（piper 無し）で本物のスクリプトを走らせると unavailable になる。"""
    _script, model = voice_files
    with pytest.raises(ProviderUnavailableError):
        await PiperVoiceGenerator.load(
            python=sys.executable,
            model_path=model,
            runner=SubprocessRunner(),
            timeout_seconds=20,
            script=SYNTHESIZE_SCRIPT,
        )


# --------------------------------------------------------------------------- 話速指定（ADR-0028）
# 音声が区間を超えたときだけ、上限つきで話速を上げて合成し直す。話速は Piper の length_scale
# （小さいほど速い）へ変換して子プロセスへ渡す。基の profile は変えない。


async def test_piper_can_synthesize_at_a_given_speed(voice_files) -> None:
    """Activity は isinstance で調整可否を決める。外れると本番で黙って調整が効かなくなる。"""
    from domain.production.ports import SpeedAdjustableVoiceGenerator

    assert isinstance(await _load(voice_files), SpeedAdjustableVoiceGenerator)


async def test_speed_is_applied_relative_to_the_voice_default_length_scale(
    voice_files, tmp_path
) -> None:
    """等速 1000 = 音声モデルの既定 length_scale。1250 ならその 1/1.25。声質は変えない。"""
    generator = await _load(voice_files, length_scale=1.1)
    out = tmp_path / "fast.wav"
    await generator.synthesize_at_speed(
        "Hello there.", "en", FileMediaDestination(out), speed_permille=1250
    )
    sent = json.loads((tmp_path / "fast.wav.args.json").read_text())
    assert sent["length_scale"] == pytest.approx(1.1 / 1.25)
    assert (sent["noise_scale"], sent["noise_w_scale"]) == (0.667, 0.8)


async def test_speed_adjustment_does_not_change_the_base_profile(voice_files, tmp_path) -> None:
    """input_hash の材料（profile）が呼び出しごとに変わらない（INV-17）。"""
    generator = await _load(voice_files)
    before = generator.generation_profile_id
    await generator.synthesize_at_speed(
        "Hello.", "en", FileMediaDestination(tmp_path / "a.wav"), speed_permille=1200
    )
    assert generator.generation_profile_id == before
    assert generator.speed_permille == 1000


async def test_speed_below_neutral_is_refused(voice_files, tmp_path) -> None:
    """遅くする用途は無い。上限つきの「速くする」調整だけを受ける。"""
    generator = await _load(voice_files)
    with pytest.raises(ValueError):
        await generator.synthesize_at_speed(
            "Hello.", "en", FileMediaDestination(tmp_path / "a.wav"), speed_permille=900
        )
