"""互換のための再輸出。

実体は ``infrastructure/temporal/run_inspector.py``（upload worker と共有 / INV-3）。
"""

from infrastructure.temporal.run_inspector import TemporalWorkflowRunInspector, WorkflowRunInspector

__all__ = ["TemporalWorkflowRunInspector", "WorkflowRunInspector"]
