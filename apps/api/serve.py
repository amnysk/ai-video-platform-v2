"""API の起動点（``python -m apps.api.serve`` / ADR-0040 §1）。

uvicorn の既定のログ設定（stderr・独自書式・access log）を使わず、共通の ``configure_logging()``
で1行1 JSON を stdout に出す。access log は ``api.request.completed`` で置き換える。
"""

from __future__ import annotations

import os

import uvicorn

from infrastructure.logging.setup import configure_logging, configure_uvicorn_loggers

HOST_ENV = "AVP_API_HOST"
PORT_ENV = "AVP_API_PORT"


def main() -> None:
    configure_logging()
    configure_uvicorn_loggers()
    uvicorn.run(
        "apps.api.main:app",
        host=os.environ.get(HOST_ENV, "0.0.0.0"),
        port=int(os.environ.get(PORT_ENV, "8000")),
        log_config=None,
        access_log=False,
    )


if __name__ == "__main__":
    main()
