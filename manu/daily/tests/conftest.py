"""pytest 共享夹具：把 `daily/` 目录挂进 `sys.path`。

【为什么需要】
`daily/` 里的模块互相引用（`main.py` → `agents_rebalance.py` → `daily_config.py`），
而 pytest 默认只把**测试文件所在目录**（`daily/tests/`）放进 `sys.path`。
⇒ 没有这个 conftest，测试里 `import notify_email` 会 ImportError。

【为什么用 `daily_config` 这个名字】
研究链里已经有 `manu/etf_rotation/config.py`（ETF 线常量），
若每日包也叫 `config.py`，两边同时进 `sys.path` 会**撞名**（后导入的覆盖前一个）
⇒ 每日包统一用 `daily_config`。
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

#: `daily/` 包目录
DAILY_DIR = Path(__file__).resolve().parents[1]
if str(DAILY_DIR) not in sys.path:
    sys.path.insert(0, str(DAILY_DIR))


@pytest.fixture()
def tmp_out_dir(tmp_path: Path) -> Path:
    """临时产物目录（测试里落盘用，不碰唯一落点根）。"""
    out = tmp_path / "out"
    out.mkdir(parents=True, exist_ok=True)
    return out
