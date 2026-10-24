"""Independent review (Agent E) defect demonstrations. NOT a fix.

Each test states the behaviour the design intends and is marked ``xfail(strict=True)``:
it fails today (proving the defect) and will turn red-as-XPASS the moment the defect is fixed,
at which point the marker should be removed. Production code is untouched.
"""

from __future__ import annotations

import importlib
import textwrap
import uuid

import pytest

from contracts.provider_health import (
    HoldReason,
    ProviderAccount,
    ProviderFamily,
)
from domain import release_preflight as rp
from infrastructure.db.provider_health_repositories import PostgresProviderHealth
from infrastructure.db.repositories import EpisodeRepository
from infrastructure.db.safety import is_destructive_statement
from tests.unit.test_release_migration_scan import _migration_status, _module

# dynamic on purpose: INV-18 forbids static fal_* imports from tests outside the allowlist
summarize_refusal_body = importlib.import_module(
    "infrastructure.providers.fal_refusal"
).summarize_refusal_body

FAL = ProviderAccount(ProviderFamily.FAL)


def test_refusal_summary_drops_query_strings_of_scheme_less_urls() -> None:
    text = "upload to //cdn.fal.media/f?sig=SECRETSIG ok"
    assert "SECRETSIG" not in summarize_refusal_body({"detail": text})


@pytest.mark.xfail(strict=True, reason="E-P2: a ';' inside a block comment hides DROP from guard")
@pytest.mark.parametrize(
    "sql",
    [
        "/* ; */ DROP TABLE episodes",
        "/* a;b */\nDROP SCHEMA public CASCADE",
        "DO $$ BEGIN EXECUTE 'DROP TABLE episodes'; END $$",
    ],
)
def test_destructive_statement_guard_sees_drop_hidden_behind_semicolon_in_comment(
    sql: str,
) -> None:
    assert is_destructive_statement(sql)


_PK_DECL = """
CHECK_WIDENING = {
    "table": "episodes",
    "constraint": "episodes_pkey",
    "column": "kind",
    "old": ("A",),
    "new": ("A", "B"),
}
"""
_PK_BODY = """
with op.batch_alter_table("episodes") as batch:
    batch.drop_constraint("episodes_pkey", type_="check")
    batch.create_check_constraint("episodes_pkey", "kind IN ('A', 'B')")
"""

_RECREATE_BODY = """
with op.batch_alter_table("t", recreate="always", copy_from=SMALLER_TABLE) as batch:
    batch.drop_constraint("ck_t_kind", type_="check")
    batch.create_check_constraint("ck_t_kind", "kind IN ('A', 'B', 'C')")
"""
_T_DECL = """
CHECK_WIDENING = {
    "table": "t",
    "constraint": "ck_t_kind",
    "column": "kind",
    "old": ("A", "B"),
    "new": ("A", "B", "C"),
}
"""


@pytest.mark.xfail(
    strict=True, reason="E-P2: the exemption trusts the declaration; a PK/FK/UNIQUE name passes"
)
def test_widening_exemption_cannot_drop_a_non_check_constraint() -> None:
    status = _migration_status(_module(textwrap.dedent(_PK_BODY), _PK_DECL))
    assert status is rp.Status.BLOCKED


@pytest.mark.xfail(
    strict=True, reason="E-P2: batch_alter_table keywords (recreate/copy_from) are not inspected"
)
def test_widening_exemption_rejects_batch_alter_table_that_recreates_the_table() -> None:
    status = _migration_status(_module(textwrap.dedent(_RECREATE_BODY), _T_DECL))
    assert status is rp.Status.BLOCKED


async def test_hold_of_an_episode_that_left_the_held_state_does_not_count(
    session_factory,
) -> None:
    health = PostgresProviderHealth(session_factory)
    async with session_factory() as session:
        ep = (await EpisodeRepository(session).create(topic="t")).id
        await session.commit()
    await health.hold_episode(
        episode_id=str(ep),
        scene_id=None,
        account=FAL,
        reason=HoldReason.WAITING_FOR_CREDITS,
        resumable_stage="production",
        generation_profile_id=None,
    )
    # The owner resumes through the existing POST /episodes/{id}/resume (not provider-credits),
    # the episode completes (or is cancelled). Simulated by moving it out of any held state.
    from sqlalchemy import update

    from infrastructure.db.models import EpisodeRow

    async with session_factory() as session:
        await session.execute(
            update(EpisodeRow).where(EpisodeRow.id == uuid.UUID(str(ep))).values(status="completed")
        )
        await session.commit()
    assert await health.open_hold_count() == 0
