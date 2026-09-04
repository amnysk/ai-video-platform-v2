from __future__ import annotations

import pytest


def pytest_collection_modifyitems(items) -> None:
    """tests/integration 配下は全て integration マーク扱いにする。"""
    for item in items:
        if "tests/integration/" in str(item.fspath).replace("\\", "/"):
            item.add_marker(pytest.mark.integration)
