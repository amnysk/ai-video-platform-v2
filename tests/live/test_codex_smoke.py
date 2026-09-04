"""本物の Codex CLI を **1回だけ** 呼ぶ最小の疎通テスト。

台本の中身は問わない。``GenerationResult`` が返ることだけを確かめる。
実行するには ``AVP_LIVE_CODEX=1 pytest -m live tests/live`` と明示する必要がある。
"""

from __future__ import annotations

from pathlib import Path

from domain.script.ports import GenerationRequest, GenerationResult
from infrastructure.config import Settings
from infrastructure.providers.codex_cli import CodexCliStoryGenerator, resolve_codex_binary
from infrastructure.providers.process import SubprocessRunner


async def test_codex_cli_answers_a_single_trivial_request(tmp_path: Path) -> None:
    settings = Settings()
    generator = CodexCliStoryGenerator(
        binary=resolve_codex_binary(settings.codex_binary),
        model=settings.codex_model,
        runner=SubprocessRunner(),
        workspace=tmp_path,
    )
    result = await generator.generate(
        GenerationRequest(
            episode_id="live-smoke",
            prompt='Reply with exactly this JSON object and nothing else: {"ok": true}',
            output_schema=None,
            timeout_seconds=settings.codex_timeout_seconds,
        )
    )
    assert isinstance(result, GenerationResult)
    assert result.text.strip()
    assert result.provider_id == "codex"
