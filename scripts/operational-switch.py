#!/usr/bin/env python
"""運用スイッチ（ADR-0021 / ADR-0023）を表示・切り替える。

    python scripts/operational-switch.py show
    python scripts/operational-switch.py set paused on --reason "maintenance"
    python scripts/operational-switch.py set uploads_paused off

env の ``PAUSED`` / ``UPLOADS_PAUSED`` とは OR で効く（どちらかが on なら止まる）。
"""

from __future__ import annotations

import argparse
import asyncio
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

from contracts.operations import OperationalSwitch  # noqa: E402
from infrastructure.config import Settings  # noqa: E402
from infrastructure.db.repositories import OperationalSwitchRepository  # noqa: E402
from infrastructure.db.session import session_factory_from_settings  # noqa: E402


async def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=(__doc__ or "").splitlines()[0])
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("show")
    set_cmd = sub.add_parser("set")
    set_cmd.add_argument("switch", choices=[s.value for s in OperationalSwitch])
    set_cmd.add_argument("state", choices=["on", "off"])
    set_cmd.add_argument("--reason", default=None)
    args = parser.parse_args(argv)

    settings = Settings()
    factory = session_factory_from_settings(settings)
    async with factory() as session:
        repo = OperationalSwitchRepository(session)
        if args.command == "set":
            await repo.set(OperationalSwitch(args.switch), args.state == "on", reason=args.reason)
            await session.commit()
        for switch in OperationalSwitch:
            print(f"{switch.value}: db={'on' if await repo.is_on(switch) else 'off'}")
    print(f"env PAUSED={settings.paused} UPLOADS_PAUSED={settings.uploads_paused}")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
