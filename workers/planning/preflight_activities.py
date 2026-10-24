"""日次の入口で Codex の model を確認する Activity（ADR-0058 / INV-75）。

設定ファイル（``config.toml`` と ``models_cache.json``）を読むだけで、codex を起動せず prompt も
送らない（利用枠を使わない）。Codex の設定を持つ script worker だけが答えられるので、ここに置く。
結果で日次を止めるかどうかは Workflow が決める（この Activity は判定を返すだけ / INV-4）。
"""

from __future__ import annotations

import logging
from collections.abc import Callable, Sequence

from temporalio import activity

from contracts.codex_models import (
    CODEX_MODEL_PREFLIGHT_ACTIVITY,
    CodexPreflightRequest,
    CodexPreflightResult,
)
from infrastructure.providers.codex_model_check import run_codex_preflight

logger = logging.getLogger(__name__)


class CodexPreflightActivities:
    def __init__(self, *, model_setting: str, allowed_raw: str, require_pinned: bool) -> None:
        self._model_setting = model_setting
        self._allowed_raw = allowed_raw
        self._require_pinned = require_pinned

    def all_activities(self) -> Sequence[Callable[..., object]]:
        return [self.codex_model_preflight]

    @activity.defn(name=CODEX_MODEL_PREFLIGHT_ACTIVITY)
    async def codex_model_preflight(self, request: CodexPreflightRequest) -> CodexPreflightResult:
        check = run_codex_preflight(
            model_setting=self._model_setting,
            allowed_raw=self._allowed_raw,
            require_pinned=self._require_pinned,
        )
        level = logging.ERROR if check.blocking else logging.INFO
        logger.log(level, "codex model preflight: %s (%s)", check.verdict.value, check.message)
        return CodexPreflightResult(
            verdict=check.verdict.value,
            blocking=check.blocking,
            model=check.model,
            source=check.source,
            message=check.message,
        )
