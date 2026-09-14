"""Production Voice Worker（ADR-0017 / Phase 4B）。台本シーンごとのナレーション音声を作る。

他の worker を import しない（INV-3）。workflow とは ``contracts.production_activities``
だけを共有する。
"""
