"""Piper（ローカル TTS）の ``VoiceGenerator`` adapter（ADR-0017 §5 / Phase 4B）。

piper-tts は GPL-3.0。**このモジュールは piper を import しない。** 隔離 venv の python で
``piper_cli/synthesize.py`` を子プロセスとして起動する（``scripts/setup-piper.sh``）。

失敗の写像:

- python / スクリプト / 音声モデルが無い・起動できない・終了コード 3
  → ``ProviderUnavailableError``（needs_input）
- 制限時間超過 → ``ProviderTimeoutError``（retryable）
- それ以外の非zero終了・空の出力 → ``ProviderInvocationError``（retryable）
- 音声モデルが言語を話せない → ``VoiceLanguageUnsupportedError``（needs_input。起動しない）

非課金・ローカルなので予約台帳に載せない（ADR-0017 §5）。
"""

from __future__ import annotations

import hashlib
import json
import os
import tempfile
from dataclasses import dataclass
from pathlib import Path

from domain.artifact.hashing import canonical_json_bytes, sha256_hex
from domain.errors import (
    ProviderInvocationError,
    ProviderTimeoutError,
    ProviderUnavailableError,
    VoiceLanguageUnsupportedError,
)
from domain.production.ports import MediaDestination
from infrastructure.media.destination import FileMediaDestination
from infrastructure.providers.process import ProcessRunner, ProcessTimeout

GENERATOR_ID = "piper"
#: 子プロセスとして走らせる単独スクリプト（import しない）
SYNTHESIZE_SCRIPT = Path(__file__).resolve().parent / "piper_cli" / "synthesize.py"
#: スクリプトの終了コード規約（piper_cli/synthesize.py）
EXIT_UNAVAILABLE = 3
#: エラー要約に残す stderr の上限
STDERR_LIMIT = 2000
#: Piper の出力形式（16-bit PCM WAV、モノラル）。サンプルレートは音声モデルの設定から。
SAMPLE_FORMAT = "pcm_s16le-mono"


@dataclass(frozen=True, slots=True)
class PiperVoiceProfile:
    """``generation_profile_id`` の材料。どれかが変われば別の生成とみなす。"""

    piper_tts_version: str
    voice_id: str
    model_sha256: str
    language_family: str
    sample_rate_hz: int
    length_scale: float
    noise_scale: float
    noise_w_scale: float

    def as_dict(self) -> dict[str, object]:
        # float は正準JSONを揺らすので permille の int にする
        return {
            "generator": GENERATOR_ID,
            "piper_tts": self.piper_tts_version,
            "voice_id": self.voice_id,
            "model_sha256": self.model_sha256,
            "sample_format": f"{SAMPLE_FORMAT}-{self.sample_rate_hz}",
            "length_scale_permille": _permille(self.length_scale),
            "noise_scale_permille": _permille(self.noise_scale),
            "noise_w_scale_permille": _permille(self.noise_w_scale),
        }

    @property
    def profile_id(self) -> str:
        digest = sha256_hex(canonical_json_bytes(self.as_dict()))
        return f"piper-{self.piper_tts_version}-{self.voice_id}-{digest[:16]}"


def _permille(value: float) -> int:
    return round(value * 1000)


def _sha256_file(path: Path) -> str:
    hasher = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1 << 20), b""):
            hasher.update(block)
    return hasher.hexdigest()


def _child_env() -> dict[str, str]:
    """子プロセスへ渡す環境。secret（DB・MinIO・fal の鍵）を渡さない（INV-20）。"""
    env = {"LC_ALL": "C.UTF-8", "PYTHONNOUSERSITE": "1"}
    for name in ("PATH", "HOME", "TMPDIR"):
        if name in os.environ:
            env[name] = os.environ[name]
    return env


class PiperVoiceGenerator:
    """``VoiceGenerator`` の Piper 実装。``PiperVoiceGenerator.load`` で組む。"""

    def __init__(
        self,
        *,
        python: Path,
        model_path: Path,
        config_path: Path,
        profile: PiperVoiceProfile,
        runner: ProcessRunner,
        timeout_seconds: int,
        script: Path = SYNTHESIZE_SCRIPT,
    ) -> None:
        self._python = python
        self._model_path = model_path
        self._config_path = config_path
        self._profile = profile
        self._runner = runner
        self._timeout_seconds = timeout_seconds
        self._script = script

    # ------------------------------------------------------------------ 構築

    @classmethod
    async def load(
        cls,
        *,
        python: str | Path | None,
        model_path: str | Path | None,
        runner: ProcessRunner,
        timeout_seconds: int,
        length_scale: float | None = None,
        noise_scale: float | None = None,
        noise_w_scale: float | None = None,
        script: Path = SYNTHESIZE_SCRIPT,
    ) -> PiperVoiceGenerator:
        """設定を検査し、piper-tts の版と音声モデルの sha256 から profile を確定する。"""
        if not python or not model_path:
            raise ProviderUnavailableError(
                "PIPER_PYTHON and PIPER_VOICE_PATH must be set (run scripts/setup-piper.sh)"
            )
        python_path = Path(python).expanduser()
        model = Path(model_path).expanduser()
        config = model.with_name(model.name + ".json")
        for label, path in (("python", python_path), ("script", script), ("voice model", model)):
            if not path.is_file():
                raise ProviderUnavailableError(f"piper {label} not found: {path}")
        if not config.is_file():
            raise ProviderUnavailableError(f"piper voice config not found: {config}")
        try:
            voice_config = json.loads(config.read_text(encoding="utf-8"))
            inference = voice_config.get("inference", {})
            family = str(voice_config["language"]["family"])
            sample_rate = int(voice_config["audio"]["sample_rate"])
        except (OSError, ValueError, KeyError, TypeError) as exc:
            raise ProviderUnavailableError(f"piper voice config unreadable: {config}") from exc

        version = await cls._piper_version(python_path, script, runner, timeout_seconds)
        profile = PiperVoiceProfile(
            piper_tts_version=version,
            voice_id=model.name.removesuffix(".onnx"),
            model_sha256=_sha256_file(model),
            language_family=family,
            sample_rate_hz=sample_rate,
            length_scale=length_scale
            if length_scale is not None
            else float(inference.get("length_scale", 1.0)),
            noise_scale=noise_scale
            if noise_scale is not None
            else float(inference.get("noise_scale", 0.667)),
            noise_w_scale=noise_w_scale
            if noise_w_scale is not None
            else float(inference.get("noise_w", 0.8)),
        )
        return cls(
            python=python_path,
            model_path=model,
            config_path=config,
            profile=profile,
            runner=runner,
            timeout_seconds=timeout_seconds,
            script=script,
        )

    @staticmethod
    async def _piper_version(
        python: Path, script: Path, runner: ProcessRunner, timeout_seconds: int
    ) -> str:
        try:
            result = await runner.run(
                [str(python), str(script), "--version"],
                stdin="",
                env=_child_env(),
                timeout_seconds=timeout_seconds,
            )
        except (OSError, ProcessTimeout) as exc:
            raise ProviderUnavailableError(f"cannot run piper python {python}: {exc}") from exc
        try:
            if result.returncode != 0:
                raise ValueError(result.stderr[-STDERR_LIMIT:])
            return str(json.loads(result.stdout)["piper_tts"])
        except (ValueError, KeyError, TypeError) as exc:
            raise ProviderUnavailableError(f"piper-tts is not usable via {python}: {exc}") from exc

    # ------------------------------------------------------------------ VoiceGenerator

    @property
    def generator_id(self) -> str:
        return GENERATOR_ID

    @property
    def generation_profile_id(self) -> str:
        return self._profile.profile_id

    @property
    def voice_id(self) -> str:
        return self._profile.voice_id

    @property
    def profile(self) -> PiperVoiceProfile:
        return self._profile

    @property
    def speed_permille(self) -> int:
        """話速（1/length_scale）の permille。"""
        return round(1000 / self._profile.length_scale)

    def supports_language(self, language: str) -> bool:
        return language == self._profile.language_family

    async def synthesize(self, text: str, language: str, dest: MediaDestination) -> None:
        if not self.supports_language(language):
            raise VoiceLanguageUnsupportedError(
                f"voice {self.voice_id} speaks {self._profile.language_family!r}, not {language!r}"
            )
        if isinstance(dest, FileMediaDestination):
            await self._run(text, dest.path)
            return
        with tempfile.TemporaryDirectory(prefix="piper-") as tmp:
            out = Path(tmp) / "voice.wav"
            await self._run(text, out)
            await dest.write(out.read_bytes())

    def build_argv(self) -> list[str]:
        return [str(self._python), str(self._script)]

    def build_stdin(self, text: str, output_path: Path) -> str:
        return json.dumps(
            {
                "model_path": str(self._model_path),
                "config_path": str(self._config_path),
                "text": text,
                "output_path": str(output_path),
                "length_scale": self._profile.length_scale,
                "noise_scale": self._profile.noise_scale,
                "noise_w_scale": self._profile.noise_w_scale,
            },
            ensure_ascii=False,
        )

    async def _run(self, text: str, output_path: Path) -> None:
        output_path.unlink(missing_ok=True)
        try:
            result = await self._runner.run(
                self.build_argv(),
                stdin=self.build_stdin(text, output_path),
                env=_child_env(),
                timeout_seconds=self._timeout_seconds,
            )
        except ProcessTimeout as exc:
            raise ProviderTimeoutError(
                f"piper exceeded {self._timeout_seconds}s for {len(text)} chars"
            ) from exc
        except OSError as exc:  # 実行ファイルが消えた・権限が無い
            raise ProviderUnavailableError(f"cannot start piper: {exc}") from exc
        stderr = result.stderr[-STDERR_LIMIT:].strip()
        if result.returncode == EXIT_UNAVAILABLE:
            raise ProviderUnavailableError(f"piper unavailable: {stderr}")
        if result.returncode != 0:
            raise ProviderInvocationError(f"piper exited {result.returncode}: {stderr}")
        if not output_path.is_file() or output_path.stat().st_size == 0:
            raise ProviderInvocationError("piper exited 0 but wrote no audio")


__all__ = [
    "GENERATOR_ID",
    "SYNTHESIZE_SCRIPT",
    "PiperVoiceGenerator",
    "PiperVoiceProfile",
]
