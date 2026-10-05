"""構造化ログ（ADR-0040 / docs/observability/log-contract.md）。

import しても handler は登録しない（``configure_logging()`` を呼んだ時だけ）。
OpenSearch には接続しない。アプリが書くのは stdout だけ。
"""

from infrastructure.logging.context import current_context, log_context
from infrastructure.logging.emit import emit
from infrastructure.logging.setup import configure_logging

__all__ = ["configure_logging", "current_context", "emit", "log_context"]
