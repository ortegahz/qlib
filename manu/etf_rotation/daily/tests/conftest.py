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

#: 🔴 2026-11-18 续四：**把 `daily/report.py` 占成 `sys.modules["report"]`**。
#:
#: 🔴 为什么必须占位（真跑抓到的真缺陷）：
#:    `manu/etf_rotation/v2/report.py` 与 `daily/report.py` **同名**；
#:    v2 的测试目录把 `v2/` 插到 `sys.path[0]`
#:    ⇒ 裸 `import report` 命中 **V2 那个** ⇒ 每日链的报告测试去测了**另一个模块**
#:    ⇒ 全量 pytest 里 **19~25 条假红**，而**单跑该文件全绿**（典型的「顺序依赖」）。
#:    ⇒ 更糟的是 `daily/main.py:580` 在**运行期** `import report`
#:    ⇒ 生产链也可能拿到**别链的 report 模块** ⇒ 报告渲染/落盘会走错实现。
#:
#: 🔴 为什么用「占位」而不是「另起一份副本」：
#:    `daily/main.py` 与测试都靠 `sys.modules["report"]` 拿**同一个对象**
#:    （测试要 monkeypatch `report.write_report`）⇒ 必须是**同一个模块对象**，
#:    另起副本会让 monkeypatch 打空（实测：副本 ⇒ 4 条真失败）。
import importlib.util as _ilu

#: daily 那份 report 的**绝对路径**（占位判定的锚点）
_REPORT_PATH = DAILY_DIR / "report.py"

#: 🔴 2026-11-18 续四（reviewer IMPORTANT-1）：占位必须**确定性**，不能是「谁先到谁赢」。
#:
#: 🔴 旧写法 ``if "report" not in sys.modules:`` 的真实语义是**条件式注册**：
#:    只要会话里有任何模块在 conftest 之前裸 ``import report``，占位就变成**空操作**
#:    ⇒ daily 的测试会去测**别链**的 report ⇒ 19~25 条假红（顺序依赖）。
#:    ⇒ 而且「谁先到谁赢」与本文件注释里「先占位 ⇒ 别链抢不到」**自相矛盾**。
#:
#: 🔴 现在的语义（**确定性**）：
#:    ① ``sys.modules["report"]`` **已经是 daily 那份** ⇒ **保持原对象**
#:       （monkeypatch 依赖**同一模块对象** ⇒ 另起副本会让 monkeypatch 打空）；
#:    ② 不存在、或指向**别链**（例如 ``v2/report.py``）⇒ **抢回**成 daily 那份
#:       （daily 链必须拥有 ``report`` 这个名字 ⇒ 否则 ``daily/main.py`` 的运行期
#:        ``import report`` 会拿到别链的实现 ⇒ 报告渲染/落盘走错实现）。
_REPORT_MODULE = sys.modules.get("report")
_REPORT_FILE = getattr(_REPORT_MODULE, "__file__", None) if _REPORT_MODULE is not None else None
_is_daily_report = (
    _REPORT_FILE is not None
    and Path(_REPORT_FILE).resolve() == _REPORT_PATH.resolve()
)
if not _is_daily_report:
    _spec = _ilu.spec_from_file_location("report", _REPORT_PATH)
    _mod = _ilu.module_from_spec(_spec)
    sys.modules["report"] = _mod          # 🔴 先占位 ⇒ 别链抢不到
    _spec.loader.exec_module(_mod)


@pytest.fixture()
def tmp_out_dir(tmp_path: Path) -> Path:
    """临时产物目录（测试里落盘用，不碰唯一落点根）。"""
    out = tmp_path / "out"
    out.mkdir(parents=True, exist_ok=True)
    return out
