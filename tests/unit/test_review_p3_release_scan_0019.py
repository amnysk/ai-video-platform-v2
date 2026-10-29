"""Phase 3 re-review (Sonnet E): the release scanner against the real migration 0019.

``_check_migration`` reads the approving ADR at the *deployed* revision (``c.adr_text(deployed, n)``),
so a marker that cites an ADR which is not yet deployed (ADR-0081 is new in this release) is not
honoured. EXPECTED TO FAIL at ba1e895: the migration_compat check would be BLOCKED for the release
that carries 0019 itself.
"""

from __future__ import annotations

import pathlib

from domain import release_preflight as rp
from tests.unit.test_release_preflight import (
    BASE_MIGRATION,
    OLD,
    ROOT_MIGRATION,
    SHA,
    Fake,
    _run,
    _status,
)

REPO = pathlib.Path(__file__).resolve().parents[2]


def test_review_real_0019_passes_the_release_scan_when_its_adr_ships_in_the_same_release() -> None:
    src = (REPO / "infrastructure/db/migrations/versions/0019_quality_check_records.py").read_text(
        encoding="utf-8"
    )
    adr = (REPO / "docs/decisions/0081-quality-check-records-and-required-set.md").read_text(
        encoding="utf-8"
    )
    fake = Fake(
        files={
            OLD: {"0014.py": ROOT_MIGRATION, "0015.py": BASE_MIGRATION},
            SHA: {"0014.py": ROOT_MIGRATION, "0015.py": BASE_MIGRATION, "0019.py": src},
        },
        adr_texts={(SHA, "0081"): adr},  # ADR-0081 exists in the target, not in the deployed rev
    )
    assert _status(_run(fake), "migration_compat") is rp.Status.PASS
