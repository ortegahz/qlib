"""pytest 夹具：把 V3 / V2 目录挂进 ``sys.path``（V3 用**扁平导入**，与 V2 同口径）。

🔴 **为什么必须显式挂路径**：V3 的模块用 ``import config_v3 as C``（扁平导入），
pytest 默认只把**测试文件所在目录**放进 ``sys.path`` ⇒ 没有本 conftest 时
``import universe`` 会 ImportError。

🔴 **同名模块的优先级**：V2 也有 ``splits.py``（V3 是**自己的**实现，不许 import V2）⇒
所以插入顺序必须是「V2 先插、V3 后插」⇒ ``sys.path[0]`` 是 V3 ⇒
``import splits`` 拿到的是 **V3 的**，`import labels` 拿到的是 **V2 的**
（V3 的标签模块叫 ``labels_v3.py``，刻意不撞名）。

🔴 **本机没有 qlib 数据集** ⇒ 测试全部用**合成数据**（除与 V2 对拍外），
**不联网**、**不读 /mnt/data**、**不训练**。
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

#: `manu/etf_rotation/v3/` 目录
V3_DIR = Path(__file__).resolve().parents[1]
#: `manu/etf_rotation/v2/` 目录（🔴 **只读**引用，只用于与 V2 对拍）
V2_DIR = V3_DIR.parent / "v2"

# 🔴 V2 先插、V3 后插 ⇒ V3 在 ``sys.path`` 最前 ⇒ 同名模块取 V3 的
for _path in (V2_DIR, V3_DIR):
    if str(_path) in sys.path:
        sys.path.remove(str(_path))
    sys.path.insert(0, str(_path))

import config_v3  # noqa: E402,F401
import labels_v3  # noqa: E402,F401
import neutralize  # noqa: E402,F401
import splits as v3_splits  # noqa: E402,F401
import universe  # noqa: E402,F401


def load_v2_module(module_name: str, filename: str):
    """按**文件路径**加载 V2 模块（🔴 **只读**，绝不许改 V2 源码）。

    用途只有一个：**口径对拍**（V3 的代码转换 / 前瞻收益必须与 V2 逐位一致）。
    """
    path = V2_DIR / filename
    spec = importlib.util.spec_from_file_location(module_name, path)
    if spec is None or spec.loader is None:  # pragma: no cover - 路径写错才会到这
        raise ImportError(f"🔴 无法加载 V2 模块：{path}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = module
    spec.loader.exec_module(module)
    return module
