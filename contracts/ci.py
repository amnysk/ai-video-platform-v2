"""CI の集約 job 名（唯一の宣言元。AGENTS.md §8）。

``.github/workflows/ci.yml`` の gate job の ``name:``、branch protection の required check、
release preflight の CI 判定（``domain/release_preflight.py``）が同じ値を指す。
tests/contract/test_ci_workflow_policy.py が ci.yml との一致を検査する。
"""

from __future__ import annotations

CI_GATE_JOB_NAME = "CI gate"
