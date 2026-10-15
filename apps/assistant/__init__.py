"""対話 AI アシスタントの tool adapter（JP-MAS-001 / ADR-0042、INV-48）。

介入 HTTP API（``/v1/intervention``）と ``contracts`` だけを使う。DB・Temporal・worker・
``infrastructure``・``domain`` には触れない（``tests/architecture/test_assistant_boundary.py``）。
"""
