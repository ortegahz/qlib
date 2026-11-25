"""🔴 2026-11-25 新增：coverage 阈值**口径对齐**守卫（实盘 ↔ V2 冻结回测）。

【真缺陷（此前没人守）】
`engine/weights.map_scores_to_etfs(..., min_coverage=0.5)` 的阈值是**隐式**口径：
  - ``analyze.py``（每日链分析层）调用时**没传** ``min_coverage`` ⇒ 靠函数默认值；
  - ``engine/portfolio.py``（引擎层）调用时**也没传** ⇒ 靠函数默认值；
⇒ 阈值来源**不唯一**：改 ``config.MIN_E_COVERAGE`` **改不到**这两处
⇒ 实盘与回测一旦分叉（口径漂移），**没有任何机器闸门会红**。

【2026-11-25 取证结论（实测，非推断）】
  - V2 冻结回测：``v2/run_v2.py`` → ``min_coverage=float(C.MIN_COVERAGE)``（C = ``v2/config_v2``）
    ⇒ 回测阈值 = ``v2/config_v2.MIN_COVERAGE`` = **0.5**；
  - 实盘：``config.MIN_E_COVERAGE`` = **0.5**；
  ⇒ **两链本就同口径** ⇒ 本次对齐**不改变入池、不改紧池子**
    （只把「隐式默认值」升级为「显式传参 + 有守卫的常量」）。

【本文件守三件事】
  G1 **三值同源**：``config.MIN_E_COVERAGE`` == ``v2/config_v2.MIN_COVERAGE``
      == ``engine/weights.DEFAULT_MIN_COVERAGE``（任一漂移 ⇒ 红灯）。
  G2 **两处调用点显式传参**：``analyze.py`` / ``engine/portfolio.py`` 里的
      ``map_scores_to_etfs(...)`` 调用**必须**带 ``min_coverage=`` 关键字
      （AST 源码级判定 ⇒ 不许再退回「靠默认值」）。
  G3 **行为语义**：``coverage_ok = (coverage >= min_coverage)``，含边界。

【为什么 G1/G2 用 AST 源码级判定（不 import）】
  本文件有**两个镜像位置**（``manu/etf_rotation/tests/`` 与 ``manu/tests/``），
  两棵树的 ``config.py`` 解析到的**个股线 config** 路径不同 ⇒ 根镜像在本机
  可能 import 失败 ⇒ 若把 G1/G2 挂在 import 上，本机就会**静默 skip**（守卫失效）。
  ⇒ G1/G2 **零依赖**（只读源码 AST）⇒ 任何机器上都会真跑；
  只有 G3（要真函数）在环境缺件时 skip。

【镜像纪律】
  本文件有**两个镜像位置**（``manu/etf_rotation/tests/`` 与 ``manu/tests/``），
  两者必须**逐字节一致** ⇒ 不许用固定的 ``parents[N]`` 之外的路径假设。
"""

from __future__ import annotations

import ast
import importlib.util
import sys
from pathlib import Path

import pandas as pd
import pytest

# ----------------------------------------------------------------- 定位


def _tree_root() -> Path:
    """本测试所在**树根**（含 ``analyze.py`` + ``engine/weights.py`` + ``config.py`` 的那一层）。

    🔴 两个镜像位置的 ``parents[1]`` 分别是 ``manu/etf_rotation`` 与 ``manu``
    ⇒ 各自树根，**正好**是本文件要验的那棵树。
    """
    root = Path(__file__).resolve().parents[1]
    for rel in ("analyze.py", "config.py", "engine/weights.py", "engine/portfolio.py"):
        assert (root / rel).is_file(), f"树根不对：{root}（缺 {rel}）"
    return root


TREE_ROOT = _tree_root()


def _find_v2_config() -> Path | None:
    """找 ``v2/config_v2.py``（V2 冻结回测的唯一配置源）。

    主树：``<tree_root>/v2/config_v2.py``；根镜像：``<上溯>/manu/etf_rotation/v2/config_v2.py``。
    找不到 ⇒ 返回 ``None``（调用方 skip，不许假装通过）。
    """
    for anc in (TREE_ROOT, *TREE_ROOT.parents):
        for cand in (
            anc / "v2" / "config_v2.py",
            anc / "manu" / "etf_rotation" / "v2" / "config_v2.py",
        ):
            if cand.is_file():
                return cand
    return None


V2_CONFIG = _find_v2_config()


def _module_literal(path: Path, name: str):
    """从源码 AST 里取模块级**字面量**赋值（不 import ⇒ 零环境依赖）。"""
    tree = ast.parse(path.read_text(encoding="utf-8"))
    for node in tree.body:
        if isinstance(node, ast.Assign):
            for target in node.targets:
                if isinstance(target, ast.Name) and target.id == name:
                    return ast.literal_eval(node.value)
    raise AssertionError(f"{path} 里找不到模块级常量 {name}")


CONFIG_MIN_COVERAGE = _module_literal(TREE_ROOT / "config.py", "MIN_E_COVERAGE")
WEIGHTS_DEFAULT_MIN_COVERAGE = _module_literal(
    TREE_ROOT / "engine" / "weights.py", "DEFAULT_MIN_COVERAGE"
)


# ----------------------------------------------------------------- G1：三值同源


def test_G1_config_and_weights_default_are_equal():
    """G1-a：实盘 ``config.MIN_E_COVERAGE`` == ``engine/weights.DEFAULT_MIN_COVERAGE``。"""
    assert CONFIG_MIN_COVERAGE == WEIGHTS_DEFAULT_MIN_COVERAGE, (
        f"口径漂移：config.MIN_E_COVERAGE={CONFIG_MIN_COVERAGE} "
        f"!= engine/weights.DEFAULT_MIN_COVERAGE={WEIGHTS_DEFAULT_MIN_COVERAGE}"
    )


def test_G1_v2_backtest_threshold_is_equal():
    """G1-b：V2 冻结回测实际阈值（``v2/config_v2.MIN_COVERAGE``）== 实盘阈值。

    回测侧口径 = ``v2/run_v2.py`` → ``min_coverage=float(C.MIN_COVERAGE)``（C = ``v2/config_v2``）。
    """
    if V2_CONFIG is None:
        pytest.skip("本树找不到 v2/config_v2.py（根镜像树）")
    v2_min = _module_literal(V2_CONFIG, "MIN_COVERAGE")
    assert v2_min == CONFIG_MIN_COVERAGE, (
        f"实盘与 V2 冻结回测口径漂移：实盘={CONFIG_MIN_COVERAGE} vs 回测={v2_min}"
    )


def test_G1_v2_backtest_passes_threshold_explicitly():
    """G1-c：回测链**确实**把该阈值传进去了（不是靠某个默认值蒙对）。"""
    if V2_CONFIG is None:
        pytest.skip("本树找不到 v2/config_v2.py（根镜像树）")
    run_v2 = V2_CONFIG.parent / "run_v2.py"
    assert run_v2.is_file(), f"找不到 {run_v2}"
    src = run_v2.read_text(encoding="utf-8")
    assert "min_coverage=float(C.MIN_COVERAGE)" in src, (
        "v2/run_v2.py 不再显式传 min_coverage ⇒ 回测口径来源变了"
    )


# ----------------------------------------------------------------- G2：调用点显式传参


def _map_scores_calls(path: Path) -> list[ast.Call]:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    calls = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            func = node.func
            fname = func.attr if isinstance(func, ast.Attribute) else getattr(func, "id", None)
            if fname == "map_scores_to_etfs":
                calls.append(node)
    return calls


def _kwarg(call: ast.Call, name: str):
    for kw in call.keywords:
        if kw.arg == name:
            return kw.value
    return None


def _is_min_coverage_ref(node) -> bool:
    """``C.MIN_E_COVERAGE`` / ``MIN_E_COVERAGE`` 这类「指向唯一事实源」的表达式。"""
    if isinstance(node, ast.Attribute):
        return node.attr == "MIN_E_COVERAGE"
    if isinstance(node, ast.Name):
        return node.id == "MIN_E_COVERAGE"
    return False


def _assert_call_passes_min_coverage(path: Path):
    calls = _map_scores_calls(path)
    assert calls, f"{path} 里没有 map_scores_to_etfs 调用（口径消失？）"
    for call in calls:
        value = _kwarg(call, "min_coverage")
        assert value is not None, (
            f"{path}:{call.lineno} 的 map_scores_to_etfs 调用**没传** min_coverage "
            "⇒ 又退回「靠函数默认值」的隐式口径"
        )
        ok = _is_min_coverage_ref(value)
        if not ok and isinstance(value, ast.Constant):
            ok = float(value.value) == float(CONFIG_MIN_COVERAGE)
        assert ok, (
            f"{path}:{call.lineno} 的 min_coverage 传的不是唯一事实源"
            f"（config.MIN_E_COVERAGE={CONFIG_MIN_COVERAGE}）"
        )


def test_G2_analyze_call_site_passes_min_coverage():
    """G2-a：``analyze.py``（每日链分析层）必须**显式**传 coverage 阈值。"""
    _assert_call_passes_min_coverage(TREE_ROOT / "analyze.py")


def test_G2_portfolio_call_site_passes_min_coverage():
    """G2-b：``engine/portfolio.py``（引擎层）必须**显式**传 coverage 阈值。"""
    _assert_call_passes_min_coverage(TREE_ROOT / "engine" / "portfolio.py")


# ----------------------------------------------------------------- G3：行为语义


def _load_weights_module():
    """按**路径**加载本树的 ``engine/weights.py``（避免两棵树的 ``engine`` 命名空间包互相污染）。

    同时把本树的 ``config.py`` 预置进 ``sys.modules["config"]`` ⇒ ``weights.py`` 里的
    ``import config as C`` 必然命中**本树**的 config（两棵树逐字节相同 ⇒ 值一致）。
    """
    cfg_path = TREE_ROOT / "config.py"
    spec = importlib.util.spec_from_file_location("mc_align_config", cfg_path)
    assert spec and spec.loader
    cfg = importlib.util.module_from_spec(spec)
    sys.modules["mc_align_config"] = cfg
    try:
        spec.loader.exec_module(cfg)
    except Exception as exc:  # pragma: no cover - 环境缺件（个股线 config 缺失）
        pytest.skip(f"本树 config 不可导入（{exc}）")

    old_config = sys.modules.get("config")
    sys.modules["config"] = cfg
    try:
        wspec = importlib.util.spec_from_file_location(
            "mc_align_weights", TREE_ROOT / "engine" / "weights.py"
        )
        assert wspec and wspec.loader
        weights = importlib.util.module_from_spec(wspec)
        sys.modules["mc_align_weights"] = weights
        wspec.loader.exec_module(weights)
    finally:
        if old_config is None:
            sys.modules.pop("config", None)
        else:
            sys.modules["config"] = old_config
    return weights


def test_G3_coverage_ok_is_ge_threshold():
    """G3：``coverage_ok = (coverage >= min_coverage)``（含边界等号）。"""
    weights = _load_weights_module()
    holdings = pd.DataFrame(
        {
            "etf_code": ["510300", "510300"],
            "etf_name": ["沪深300ETF", "沪深300ETF"],
            "instrument": ["SH600000", "SH600001"],
            "weight_pct": [70.0, 30.0],
        }
    )
    scores = pd.DataFrame(
        {
            "date": [pd.Timestamp("2026-11-04")],
            "instrument": ["SH600000"],
            "score": [1.0],
        }
    )
    asof = pd.Timestamp("2026-11-04")

    out = weights.map_scores_to_etfs(holdings, scores, asof)
    assert float(out.iloc[0]["coverage"]) == pytest.approx(0.7)
    assert bool(out.iloc[0]["coverage_ok"]) is True

    def ok_at(threshold: float) -> bool:
        res = weights.map_scores_to_etfs(holdings, scores, asof, min_coverage=threshold)
        return bool(res.iloc[0]["coverage_ok"])

    assert ok_at(0.45) is True
    assert ok_at(0.70) is True  # 边界：**含**等号（>=）
    assert ok_at(0.71) is False
