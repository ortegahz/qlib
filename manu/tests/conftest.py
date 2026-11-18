"""pytest 夹具：把 `manu/etf_rotation/` 挂进 `sys.path`（`import qlib_scores` 就能找到）。

**为什么需要**：`qlib_scores.py` 自己在模块顶部把**自身目录**与 `data/` 插进 `sys.path`
（为了 `import config` / `import signal_policy`），但 pytest 默认只把**测试文件所在目录**
放进 `sys.path` ⇒ 没有本 conftest 时 `import qlib_scores` 会 ImportError。

**不依赖 qlib 运行时**：`qlib_scores.py` 的 qlib 依赖全在函数内部（`init_qlib` /
`build_*_handler` / `model.predict`），模块级只需要 numpy + pandas ⇒ 单测可离线跑。
"""
from __future__ import annotations

import sys
from pathlib import Path

#: `manu/etf_rotation/` 目录
ETF_DIR = Path(__file__).resolve().parents[1]
if str(ETF_DIR) not in sys.path:
    sys.path.insert(0, str(ETF_DIR))
