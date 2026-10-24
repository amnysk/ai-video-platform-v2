"""integration テストが使う PostgreSQL の URL と、破壊的操作の安全装置（ADR-0021 / ADR-0055）。

規則の本体は ``infrastructure/db/safety.py`` の1か所（AGENTS §8）。ここは再公開だけで、
規則を重複して持たない。未設定の ``TEST_DATABASE_URL`` だけが skip で、条件を満たさない URL は例外。
"""

from __future__ import annotations

from infrastructure.db.safety import (
    LOCAL_HOSTS,
    TEST_SUFFIX,
    UnsafeDatabaseError,
    UnsafeTestDatabaseError,
    assert_destructive_allowed,
    guarded_create_all,
    guarded_drop_all,
    require_test_database_url,
    validate_test_database_url,
)

__all__ = [
    "LOCAL_HOSTS",
    "TEST_SUFFIX",
    "UnsafeDatabaseError",
    "UnsafeTestDatabaseError",
    "assert_destructive_allowed",
    "guarded_create_all",
    "guarded_drop_all",
    "require_test_database_url",
    "validate_test_database_url",
]
