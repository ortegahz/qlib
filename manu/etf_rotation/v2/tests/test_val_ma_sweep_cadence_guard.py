"""🔴 V2 **MA 二次扫工具**的 cadence fail-closed 守卫测试 —— 2026-11-17 夜班审计补。

**背景（审计挖出的第 4 处 fail-open）**
  `manu/tools/v2_val_ma_sweep.py` 解析第一轮搜索产物的 cadence 时用的是
  ``base.get("rebalance_every_n_days", C.REBALANCE_EVERY_N_DAYS)``
  ⇒ **搜索产物缺 cadence 键时静默回落常量 20**。
  而 `run_v2.py:stage_freeze` 的同源校验读的是 MA 扫产物里的
  ``fixed_from_round1["rebalance_every_n_days"]`` ⇒ 那里拿到的是**回落值 20**
  ⇒ **校验形同虚设** ⇒ 「cadence=20 上下文里扫出来的 MA 窗口」会和
  「别的 cadence 下选出的其余参数」一起被冻进冻结产物
  ⇒ 冻结参数**内部不自洽** ⇒ 白烧掉**一次性**单盲 token。

**本文件钉死**
  ① cadence 键缺失 ⇒ MA 扫工具**必须硬报错**（不许回落常量）；
  ② 【变异反证】把硬报错改回落常量 ⇒ ①必须转红（证明这道闸门有牙）；
  ③ cadence 键存在 ⇒ 必须原样透传（不许误杀正路）。

⛔ **只用自造夹具** ⇒ 不联网、不跑重量级回测、不读真实行情。
⛔ **绝不落盘改真源码**（变异 = 源码 → 内存改 → tmp_path → import 副本）。
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest

_PROJ = Path(__file__).resolve().parents[4]
_TOOL_SRC = _PROJ / "manu" / "tools" / "v2_val_ma_sweep.py"


def _load_module(path: Path, tag: str):
    """🔴 从源码路径 import 一个模块副本（可指向 tmp_path 里的变异体）。"""
    spec = importlib.util.spec_from_file_location(tag, path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[tag] = module
    spec.loader.exec_module(module)
    return module


@pytest.fixture(scope="module")
def tool():
    return _load_module(_TOOL_SRC, "v2_val_ma_sweep_real")


def _mutant(tmp_path: Path) -> object:
    """🔴 把「缺 cadence 硬报错」改回落常量 20（旧口径）⇒ 变异体。"""
    text = _TOOL_SRC.read_text(encoding="utf-8")
    old = '    if "rebalance_every_n_days" not in base:\n        raise RuntimeError(\n'
    new = ('    if "rebalance_every_n_days" not in base:\n'
           '        return int(base.get("rebalance_every_n_days", 20))\n'
           '    if False:\n'
           '        raise RuntimeError(\n')
    assert old in text, "🔴 变异锚点不存在（源码已变）"
    text = text.replace(old, new)
    target = tmp_path / "mutant_v2_val_ma_sweep.py"
    target.write_text(text, encoding="utf-8")
    return _load_module(target, "mutant_v2_val_ma_sweep")


# ---------------------------------------------------------------- ① 缺 cadence ⇒ 硬报错
def test_MA扫工具缺cadence_必须硬报错(tool) -> None:
    """🔴 第一轮搜索产物没有 cadence 键 ⇒ **必须抛错**，不许静默回落常量 20。"""
    with pytest.raises(RuntimeError) as exc:
        tool.resolve_cadence({"smoothing_arm": "A_half_life_20", "top_k": 5})
    assert "rebalance_every_n_days" in str(exc.value), (
        f"🔴 报错必须点名 cadence 字段：{exc.value!r}")


def test_变异反证_回落常量_硬报错必须转红(tmp_path: Path, tool) -> None:
    """🔴 把「缺 cadence 硬报错」改回落常量 20 ⇒ ①必须转红（证明这道闸门有牙）。"""
    mutant = _mutant(tmp_path)
    # 真源码：必须报错
    with pytest.raises(RuntimeError):
        tool.resolve_cadence({})
    # 变异体：**不许**再报错（否则硬报错没被测到）
    assert mutant.resolve_cadence({}) == 20, (
        "🔴 变异体应当回落到常量 20（否则这条变异反证没意义）")


# ---------------------------------------------------------------- ② 有 cadence ⇒ 原样透传
def test_MA扫工具有cadence_必须原样透传(tool) -> None:
    """🔴 cadence 键存在 ⇒ 必须原样返回（不许误杀正路）。"""
    assert tool.resolve_cadence({"rebalance_every_n_days": 40}) == 40
    assert tool.resolve_cadence({"rebalance_every_n_days": 90}) == 90


# ---------------------------------------------------------------- ③ 源码里不许再有静默回落
def test_源码里不许再有静默回落常量(tool) -> None:
    """🔴 除了 ``resolve_cadence`` 的**文档字符串**外，源码里不许再出现
    ``base.get("rebalance_every_n_days", C.REBALANCE_EVERY_N_DAYS)`` 式的回落。"""
    src = _TOOL_SRC.read_text(encoding="utf-8")
    lines = src.splitlines()
    hits = [i for i, ln in enumerate(lines) if "C.REBALANCE_EVERY_N_DAYS" in ln]
    assert hits, "🔴 锚点失效：源码里连旧回落写法都找不到了（请同步改测试）"
    doc_start = next(i for i, ln in enumerate(lines) if ln.startswith("def resolve_cadence"))
    doc_end = next(i for i, ln in enumerate(lines) if ln.startswith("def main()"))
    for i in hits:
        assert doc_start < i < doc_end, (
            f"🔴 第 {i + 1} 行仍有静默回落写法（必须在 resolve_cadence 的说明之外）：{lines[i]!r}")
