"""Production Workflow（ADR-0017）。工程の順序・ラウンド・並行数を決め、状態系 Activity を持つ。

メディア別 worker（``workers.production_image`` 等）を import しない（INV-3）。
それらの Activity は ``contracts.production_activities`` の**名前**で呼ぶ。
"""
