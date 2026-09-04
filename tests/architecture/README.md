# architecture tests

不変条件のうち、コードの形として検査できるものをここで強制する。
これらは Phase 0 の時点で既に有効であり、実装が入る前から落ちる準備ができている。

未実装（Phase 1）:
- `test_api_no_heavy_work.py` — INV-16
- `test_no_live_calls.py` — INV-18
