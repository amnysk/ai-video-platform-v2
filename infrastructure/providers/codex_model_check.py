"""Codex に渡る model の事前検査（起動時）。

実際に使われる model は ``CODEX_MODEL``（``-m``）> ``~/.codex/config.toml`` の順。
許可リストは ``contracts.codex_models`` の ``CODEX_ALLOWED_MODELS`` だけが出所で、
**既定は空（未検証）**。空のときは「使える」と主張せず警告に留める。
"""

from __future__ import annotations

import logging
import os
import tomllib
from pathlib import Path

from domain.errors import UnsupportedModelError

logger = logging.getLogger(__name__)


def default_config_toml() -> Path:
    home = os.environ.get("CODEX_HOME") or str(Path(os.environ.get("HOME", "~")) / ".codex")
    return Path(home).expanduser() / "config.toml"


def configured_model_from_toml(path: Path) -> str | None:
    """トップレベルの ``model`` だけを読む。読めなければ ``None``（検査を止めない）。"""
    try:
        data = tomllib.loads(path.read_text(encoding="utf-8"))
    except (OSError, tomllib.TOMLDecodeError):
        return None
    value = data.get("model")
    return value if isinstance(value, str) and value else None


def effective_model(model_setting: str, config_toml: Path) -> str | None:
    return model_setting or configured_model_from_toml(config_toml)


def verify_codex_model(
    *, model_setting: str, allowed: frozenset[str], config_toml: Path | None = None
) -> None:
    """許可リストがあり、model がその外なら ``UnsupportedModelError``。"""
    model = effective_model(model_setting, config_toml or default_config_toml())
    if model is None:
        logger.warning("codex model: not pinned and not readable from config.toml (unverified)")
        return
    if not allowed:
        logger.warning(
            "codex model %r is unverified: CODEX_ALLOWED_MODELS is empty "
            "(see docs/operations/codex-model-config.md)",
            model,
        )
        return
    if model not in allowed:
        raise UnsupportedModelError(
            f"codex model {model!r} is not in CODEX_ALLOWED_MODELS ({', '.join(sorted(allowed))})",
            model=model,
        )
    logger.info("codex model %r is in the allowlist", model)
