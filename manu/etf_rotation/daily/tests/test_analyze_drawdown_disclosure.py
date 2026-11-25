# -*- coding: utf-8 -*-
"""🔴 **风险披露「组合级回撤刹车」文案**的**口径回归网**（2026-11-25 · 点 4b）

【为什么要有这个文件】
  2026-11-24 夜 V2 full 交付报告暴露的报表小瑕疵之一：
  风险披露里的 ``no_drawdown_brake`` 仍**静态**写着「仍**没有组合级硬止损**」，
  而同一份报告的流水线备注里**组合级 -15% 回撤熔断已实装**
  （``daily/nav_state.py`` ＋ ``daily/daily_config.py::DRAWDOWN_BRAKE_THRESHOLD=0.15``）
  ⇒ **新旧披露文案打架** ⇒ 读者会以为急跌仍然裸奔。

  修复口径：**按事实派生**，不是按"实装没实装"写死两套。
    * 有 ``nav_state`` ⇒ 标题「组合级回撤刹车已接线（急跌不再裸奔）」、
      ``severity=medium``、detail 写出**净值 / 峰值 / 回撤% / 阈值 / 是否触发**；
    * 无 ``nav_state``（研究链单跑、净值不可得）⇒ **保留旧的诚实文案**
      （``severity=high``、明说「没有组合级硬止损」）。

【本文件覆盖三件事】
  ① **有 nav_state** ⇒ 派生文案生效（数字齐全、**不再**出现「没有组合级硬止损」）；
  ② **无 nav_state** ⇒ 旧文案**逐字保留**（不许因为改了分支就丢诚实披露）；
  ③ **变异反证**：把活链 ``analyze.py`` 的派生分支改坏（``if _nav is not None
     and _peak is not None:`` ⇒ ``if False:``）到**临时副本**里 ⇒ ① 必须转红
     （证明这条网有牙齿，不是恒真）。

【🔴 口径边界】
  * 本文件**只测披露文案派生**，不跑 qlib / 不读生产快照；
  * ``no_drawdown_brake`` 的 **id 保持不变**（探针按 id 取值：
    ``probe_microstructure`` / ``probe_lookahead`` / ``probe_snapshot_lookahead``）
    ⇒ 本文件同时把「id 不得改」钉死。
"""
from __future__ import annotations

import importlib.util
import shutil
import sys
from pathlib import Path

import pandas as pd
import pytest

#: ``manu/etf_rotation/``（研究链根）—— 本文件在 ``daily/tests/`` 下 ⇒ 上溯两级
_ETF_DIR = Path(__file__).resolve().parents[2]
#: 🔴 必须排在**最前**：``manu/`` 与 ``manu/etf_rotation/`` 都**没有** ``__init__.py``
#:    ⇒ 同名模块（``analyze.py``）的解析结果**取决于 sys.path 顺序**
#:    ⇒ 显式把**活链**顶到最前，避免测到镜像副本。
sys.path.insert(0, str(_ETF_DIR))

import config as C  # noqa: E402

#: 活链 ``analyze.py`` 的绝对路径
_ANALYZE_SRC = _ETF_DIR / "analyze.py"


def _load_by_path(name: str, path: Path):
    """🔴 按**绝对路径**加载模块（同名遮蔽免疫）。"""
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec is not None and spec.loader is not None, f"加载失败：{path}"
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


_ANALYZE = _load_by_path("etf_rotation_analyze_drawdown_target", _ANALYZE_SRC)

#: 🔴 实测口径（2026-11-24 夜跑批的账户净值事实，**不许编**）：
#:    净值 30,478.90 / 峰值 30,502.00（11-20）⇒ 回撤 −0.0757% ⇒ 阈值 −15% ⇒ 未触发。
_NAV = 30478.90
_PEAK = 30502.00
_DD = _NAV / _PEAK - 1.0  # ≈ -0.000757

#: 真值阈值（单一事实源 = ``config.DRAWDOWN_BRAKE_THRESHOLD``）
_THRESH = float(C.DRAWDOWN_BRAKE_THRESHOLD)


def _risk(nav_state: dict | None) -> list[dict]:
    """调用**活链** ``_risk_disclosure``，返回风险条目的最小夹具调用。"""
    return _ANALYZE._risk_disclosure(
        asof=pd.Timestamp("2026-11-24"),
        chain={"pool_mode": "e_covered", "hold_buffer": 0.05},
        n_etfs_no_e=3,
        n_universe_rows=1664,
        n_pool=18,
        holdings=[],
        total_assets=30425.00,
        cash=23371.80,
        backtest=None,
        score_stats=None,
        rank_ic=None,
        micro=None,
        lookahead=None,
        ma_windows=(0, 0),
        nav_state=nav_state,
    )


def _item(risk: list[dict], rid: str = "no_drawdown_brake") -> dict:
    got = [r for r in risk if str(r.get("id")) == rid]
    assert got, f"风险披露里没有 id={rid} 的条目"
    return got[0]


# ---------------------------------------------------------------- §1 有 nav_state
def test_nav_state_present_derives_derived_wording() -> None:
    """有 nav_state ⇒ 派生文案生效 + 数字齐全 + 旧假话消失。"""
    risk = _risk({
        "nav": _NAV,
        "peak": _PEAK,
        "drawdown": _DD,
        "threshold": _THRESH,
        "in_brake": False,
        "cooldown_left": 0,
    })
    it = _item(risk)
    detail = str(it.get("detail") or "")
    title = str(it.get("title") or "")

    assert it.get("severity") == "medium", f"severity 应降为 medium，实得 {it.get('severity')!r}"
    assert "已接线" in title, f"标题应改为「已接线」，实得 {title!r}"
    assert "组合级回撤刹车已实装" in detail, "detail 必须明写刹车已实装"
    # 🔴 数字不许缺（净值 / 峰值 / 回撤 / 阈值）
    assert "30478.90" in detail, f"detail 缺净值数字：{detail!r}"
    assert "30502.00" in detail, f"detail 缺峰值数字：{detail!r}"
    assert "-15%" in detail, f"detail 缺阈值 -15%：{detail!r}"
    assert f"{_DD:+.2%}" in detail, f"detail 缺回撤%：{detail!r}"
    assert "未触发" in detail, f"detail 必须写明本轮未触发：{detail!r}"
    # 🔴 旧假话必须消失
    assert "没有组合级硬止损" not in detail, "净值可得时不许再写「没有组合级硬止损」"
    # 🔴 id 不许改（探针按 id 取值）
    assert it.get("id") == "no_drawdown_brake"


def test_nav_state_in_brake_reports_triggered() -> None:
    """in_brake=True ⇒ detail 必须写「已触发」（不许含糊）。"""
    risk = _risk({
        "nav": _PEAK * 0.80,
        "peak": _PEAK,
        "drawdown": -0.20,
        "threshold": _THRESH,
        "in_brake": True,
        "cooldown_left": 0,
    })
    detail = str(_item(risk).get("detail") or "")
    assert "已触发" in detail, f"detail 必须写明已触发：{detail!r}"
    assert "未触发" not in detail, f"已触发时不许出现「未触发」：{detail!r}"


# ---------------------------------------------------------------- §2 无 nav_state
def test_no_nav_state_keeps_honest_old_wording() -> None:
    """无 nav_state ⇒ **保留旧的诚实文案**（severity=high + 明说没有硬止损）。"""
    it = _item(_risk(None))
    detail = str(it.get("detail") or "")
    assert it.get("severity") == "high", f"净值不可得时 severity 应保持 high，实得 {it.get('severity')!r}"
    assert "急跌无刹车" in str(it.get("title") or ""), f"标题应保留旧文案：{it.get('title')!r}"
    assert "没有组合级硬止损" in detail, "净值不可得时必须保留「没有组合级硬止损」的诚实披露"
    assert "组合级回撤刹车已实装" not in detail, "净值不可得时不许吹「已实装」"


# ---------------------------------------------------------------- §3 变异反证
def test_mutation_breaks_derived_branch(monkeypatch, tmp_path: Path) -> None:
    """🔴 把派生分支改坏 ⇒ §1 必须转红（证明这条网有牙齿）。"""
    src = _ANALYZE_SRC.read_text(encoding="utf-8")
    needle = "if _nav is not None and _peak is not None:"
    assert needle in src, "🔴 活链里找不到派生分支的判据行 ⇒ 源码结构变了，本文件的变异反证失效"
    mutated = src.replace(needle, "if False:", 1)
    assert mutated != src, "变异未生效"

    tmp_src = tmp_path / "analyze_mutated.py"
    tmp_src.write_text(mutated, encoding="utf-8")
    mut = _load_by_path("etf_rotation_analyze_drawdown_mutant", tmp_src)
    try:
        with pytest.raises(AssertionError):
            risk = mut._risk_disclosure(
                asof=pd.Timestamp("2026-11-24"),
                chain={"pool_mode": "e_covered", "hold_buffer": 0.05},
                n_etfs_no_e=3,
                n_universe_rows=1664,
                n_pool=18,
                holdings=[],
                total_assets=30425.00,
                cash=23371.80,
                backtest=None,
                score_stats=None,
                rank_ic=None,
                micro=None,
                lookahead=None,
                ma_windows=(0, 0),
                nav_state={
                    "nav": _NAV,
                    "peak": _PEAK,
                    "drawdown": _DD,
                    "threshold": _THRESH,
                    "in_brake": False,
                    "cooldown_left": 0,
                },
            )
            it = _item(risk)
            detail = str(it.get("detail") or "")
            assert "组合级回撤刹车已实装" in detail, "变异后应回落到旧文案 ⇒ 断言必须红"
            assert "没有组合级硬止损" not in detail, "变异后应回落到旧文案 ⇒ 断言必须红"
    finally:
        sys.modules.pop("etf_rotation_analyze_drawdown_mutant", None)
        shutil.rmtree(tmp_path, ignore_errors=True)
