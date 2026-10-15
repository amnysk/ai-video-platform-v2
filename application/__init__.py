"""Application 層（ADR-0043 §1）。入口（HTTP・CLI・アシスタント・内部 Agent）が共通に通るユースケース。

import してよいのは ``contracts`` と ``domain`` だけ（INV-6）。
"""
